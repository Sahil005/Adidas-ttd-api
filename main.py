"""
Cloud Function: TTD Spike Budget Update
Triggers: HTTP request or Cloud Scheduler
Purpose: Check for Spike triggers in BigQuery and update TTD campaign budgets based on date-configured budget periods
"""

from datetime import datetime, timezone
import json
import os
import pytz
import requests
import pandas as pd

from google.cloud import bigquery
from google.cloud import secretmanager
from google.auth import default as google_auth_default
from flask import Request

# ============ CONFIGURATION ============

PROJECT_ID = os.getenv("PROJECT_ID", "acceleration-australia")
DATASET_ID = os.getenv("DATASET_ID", "adidas_ttd")
TABLE_ID = os.getenv("TABLE_ID", "sb_campaign_budget_updates")
FULL_TABLE_ID = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"

SECRET_ID = os.getenv("SECRET_ID", "ttd-s")
REST_URL = os.getenv("REST_URL", "https://api.thetradedesk.com/v3")
GQL_URL = os.getenv("GQL_URL", "https://api.thetradedesk.com/graphql")

CAMPAIGN_TZ = pytz.timezone("Australia/Sydney")


def get_bq_client() -> bigquery.Client:
    """Create BigQuery client with Drive scope for external Google Sheets tables."""
    scopes = [
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    credentials, _ = google_auth_default(scopes=scopes)
    return bigquery.Client(project=PROJECT_ID, credentials=credentials)

# ============ WEEKLY PLAN + ROLLOVER CONFIG ============

CAMPAIGN_WEEKLY_PLAN = {
    "81ub60c": [
        {"week": 1, "week_start": "2026-06-17", "week_end": "2026-06-23", "half": 1, "planned_moments": 3, "base_budget_per_moment": 798.64},
        {"week": 2, "week_start": "2026-06-24", "week_end": "2026-06-30", "half": 1, "planned_moments": 3, "base_budget_per_moment": 798.64},
        {"week": 3, "week_start": "2026-07-01", "week_end": "2026-07-07", "half": 1, "planned_moments": 3, "base_budget_per_moment": 798.64},
        {"week": 4, "week_start": "2026-07-08", "week_end": "2026-07-14", "half": 2, "planned_moments": 2, "base_budget_per_moment": 2695.42},
        {"week": 5, "week_start": "2026-07-15", "week_end": "2026-07-21", "half": 2, "planned_moments": 2, "base_budget_per_moment": 2695.42},
    ],
    "51tk9dq": [
        {"week": 1, "week_start": "2026-06-17", "week_end": "2026-06-23", "half": 1, "planned_moments": 3, "base_budget_per_moment": 1958.22},
        {"week": 2, "week_start": "2026-06-24", "week_end": "2026-06-30", "half": 1, "planned_moments": 3, "base_budget_per_moment": 1958.22},
        {"week": 3, "week_start": "2026-07-01", "week_end": "2026-07-07", "half": 1, "planned_moments": 3, "base_budget_per_moment": 1958.22},
        {"week": 4, "week_start": "2026-07-08", "week_end": "2026-07-14", "half": 2, "planned_moments": 2, "base_budget_per_moment": 3604.9},
        {"week": 5, "week_start": "2026-07-15", "week_end": "2026-07-21", "half": 2, "planned_moments": 2, "base_budget_per_moment": 3604.9},
    ],
}

# ============ AUTHENTICATION ============

def get_secret_value(project_id: str, secret_id: str, version: str = "latest") -> str:
    """Retrieve secret from GCP Secret Manager"""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8").strip()

try:
    TOKEN = get_secret_value(PROJECT_ID, SECRET_ID)
except Exception as e:
    print(f"[CF] Warning: Could not load token from Secret Manager: {e}")
    TOKEN = os.getenv("TTD_TOKEN", "")

headers = {
    "TTD-Auth": TOKEN,
    "Content-Type": "application/json"
}

# ============ API HELPERS ============

def get_campaign_details(campaign_id: str) -> dict:
    """Fetch campaign details from TTD REST API"""
    url = f"{REST_URL}/campaign/{campaign_id}"
    response = requests.get(url, headers=headers, timeout=60)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch campaign details for {campaign_id}: {response.text}")
    return response.json()

def _parse_ttd_datetime(value):
    """Parse TTD datetime format"""
    if value is None:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    return datetime.fromisoformat(text).astimezone(timezone.utc)

def select_target_flight_id(campaign_data: dict, preferred_flight_id=None) -> int:
    """Select active flight ID or first available"""
    if preferred_flight_id is not None and not pd.isna(preferred_flight_id):
        return int(preferred_flight_id)

    flights = campaign_data.get("CampaignFlights", [])
    if not flights:
        raise Exception("No campaign flights found.")

    now_utc = datetime.now(timezone.utc)
    for flight in flights:
        flight_id = flight.get("CampaignFlightId")
        start_dt = _parse_ttd_datetime(flight.get("StartDateInclusiveUTC"))
        end_dt = _parse_ttd_datetime(flight.get("EndDateExclusiveUTC"))

        if flight_id is None or start_dt is None:
            continue

        is_active = start_dt <= now_utc and (end_dt is None or now_utc < end_dt)
        if is_active:
            return int(flight_id)

    return int(flights[0]["CampaignFlightId"])

def update_kokai_budget(
    campaign_id: str,
    flight_id: int,
    daily_budget: float,
    total_budget: float,
) -> bool:
    """Update campaign budget via GraphQL"""
    mutation = """
    mutation UpdateKokaiBudgetSettings(
      $campaignId: ID!,
      $currentFlightId: Long!,
      $totalBudget: Decimal!,
      $dailyBudget: Decimal!
    ) {
      campaignBudgetSettingsUpdate(
        input: {
          campaignId : $campaignId,
          pacingMode : PACE_TO_DAILY_CAP,
          campaignFlights : [{
            campaignFlightId: $currentFlightId,
            budgetInAdvertiserCurrency: $totalBudget,
            dailyTargetInAdvertiserCurrency: $dailyBudget
          }]
        }
      ) {
        data {
          wasBudgetUpdated
        }
        userErrors {
          field
          message
        }
      }
    }
    """

    variables = {
        "campaignId": campaign_id,
        "currentFlightId": int(flight_id),
        "totalBudget": float(total_budget),
        "dailyBudget": float(daily_budget),
    }

    payload = {"query": mutation, "variables": variables}
    response = requests.post(GQL_URL, headers=headers, json=payload, timeout=60)

    if response.status_code != 200:
        raise Exception(f"GraphQL request failed: {response.text}")

    response_data = response.json()

    if "errors" in response_data:
        raise Exception(json.dumps(response_data["errors"], indent=2))

    result = response_data["data"]["campaignBudgetSettingsUpdate"]
    if result["userErrors"]:
        raise Exception(json.dumps(result["userErrors"], indent=2))

    return result["data"]["wasBudgetUpdated"]


def get_campaign_adgroups(campaign_id: str):
    """Fetch ad groups for a campaign."""
    url = f"{REST_URL}/adgroup/query/campaign"
    body = {
        "CampaignId": campaign_id,
        "PageSize": 10000,
        "PageStartIndex": 0,
    }
    response = requests.post(url, headers=headers, json=body, timeout=60)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch ad groups for {campaign_id}: {response.text}")
    return response.json().get("Result", [])


def _update_single_adgroup_daily_cap(ad_group_id, daily_budget: float):
    """Update ad group daily budget cap. TTD expects nested RTBAttributes payload."""
    ad_group_id_str = str(ad_group_id).strip()

    payload_options = [
        {
            "AdGroupId": ad_group_id_str,
            "RTBAttributes": {
                "BudgetSettings": {
                    "DailyBudget": {
                        "Amount": float(daily_budget),
                        "CurrencyCode": "AUD",
                    }
                }
            },
        },
        {
            "AdGroupId": ad_group_id_str,
            "DailySpendCapInAdvertiserCurrency": float(daily_budget),
        },
    ]

    last_error = None
    for payload in payload_options:
        try:
            response = requests.put(f"{REST_URL}/adgroup", headers=headers, json=payload, timeout=60)
            if response.status_code in (200, 201, 204):
                return True
            last_error = f"PUT /adgroup -> {response.status_code}: {response.text}"
        except Exception as err:
            last_error = f"PUT /adgroup -> {err}"

    raise Exception(last_error or f"Unable to update ad group {ad_group_id_str}")


def update_campaign_adgroup_daily_caps(campaign_id: str, daily_budget: float):
    """Apply the same daily cap to all ad groups in the campaign."""
    adgroups = get_campaign_adgroups(campaign_id)
    updated = 0
    errors = []

    for adg in adgroups:
        ad_group_id = adg.get("AdGroupId") or adg.get("Id")
        if ad_group_id is None:
            continue

        try:
            _update_single_adgroup_daily_cap(ad_group_id, float(daily_budget))
            updated += 1
        except Exception as err:
            errors.append(f"{ad_group_id}: {err}")

    return {
        "count": len(adgroups),
        "updated": updated,
        "errors": errors,
    }


def get_selected_flight_total_budget(campaign_data: dict, flight_id: int) -> float:
    target_flight = next(
        (
            flight
            for flight in campaign_data.get("CampaignFlights", [])
            if int(flight.get("CampaignFlightId", -1)) == int(flight_id)
        ),
        None,
    )

    if target_flight is None:
        raise Exception(f"Flight {flight_id} not found in campaign")

    return float(target_flight["BudgetInAdvertiserCurrency"])


def set_daily_target_zero_keep_total(campaign_id: str, flight_id: int) -> tuple[float, dict]:
    """Set campaign + adgroups daily cap to 0 while keeping current flight total unchanged."""
    campaign_data = get_campaign_details(campaign_id)
    current_total = get_selected_flight_total_budget(campaign_data, flight_id)

    update_kokai_budget(
        campaign_id=campaign_id,
        flight_id=flight_id,
        daily_budget=0.0,
        total_budget=current_total,
    )

    adgroup_result = update_campaign_adgroup_daily_caps(campaign_id, daily_budget=0.0)
    if adgroup_result["errors"]:
        print(f"[CF] ⚠ {campaign_id} | adgroup cap update partial failure: {adgroup_result['errors'][:2]}")

    return current_total, adgroup_result

def _to_date(text: str):
    return datetime.strptime(text, "%Y-%m-%d").date()


def _week_for_date(campaign_id: str, target_date):
    for week_cfg in CAMPAIGN_WEEKLY_PLAN[campaign_id]:
        if _to_date(week_cfg["week_start"]) <= target_date <= _to_date(week_cfg["week_end"]):
            return week_cfg
    return None


def _count_spikes(df: pd.DataFrame, start_date, end_date) -> int:
    """Count unique spike dates in the provided range (one date = one moment)."""
    working_df = df.copy()
    working_df["Date_parsed"] = pd.to_datetime(working_df["Date"], errors="coerce", dayfirst=True)
    working_df["Date_only"] = working_df["Date_parsed"].dt.date
    working_df["Trigger_norm"] = working_df["Trigger"].astype(str).str.strip().str.lower()

    daily_spikes = working_df[
        (working_df["Trigger_norm"] == "spike") &
        (working_df["Date_only"] >= start_date) &
        (working_df["Date_only"] <= end_date)
    ]["Date_only"].dropna().drop_duplicates()

    return int(len(daily_spikes))


def get_rollover_budget_for_campaign(campaign_id: str, df: pd.DataFrame, target_date=None):
    """
    Compute daily moment budget with rollover:
    - Unused weekly budget rolls to remaining weeks of same half.
    - Leftover from first half rolls into remaining weeks of second half.
    """
    if target_date is None:
        target_date = datetime.now(CAMPAIGN_TZ).date()

    if campaign_id not in CAMPAIGN_WEEKLY_PLAN:
        raise ValueError(f"Campaign {campaign_id} not found in weekly plan")

    current_week = _week_for_date(campaign_id, target_date)
    if current_week is None:
        return None

    plan = CAMPAIGN_WEEKLY_PLAN[campaign_id]

    week_stats = []
    for week_cfg in plan:
        ws = _to_date(week_cfg["week_start"])
        we = _to_date(week_cfg["week_end"])
        actual_moments = _count_spikes(df, ws, we)
        planned_moments = int(week_cfg["planned_moments"])
        consumed_moments = min(actual_moments, planned_moments)
        unused_moments = max(planned_moments - consumed_moments, 0)
        base = float(week_cfg["base_budget_per_moment"])

        week_stats.append({
            **week_cfg,
            "actual_moments": actual_moments,
            "consumed_moments": consumed_moments,
            "unused_moments": unused_moments,
            "planned_budget": planned_moments * base,
            "consumed_budget": consumed_moments * base,
            "unused_budget": unused_moments * base,
        })

    current_week_num = current_week["week"]
    current_half = current_week["half"]

    rollover_same_half = sum(
        w["unused_budget"]
        for w in week_stats
        if w["half"] == current_half and w["week"] < current_week_num
    )

    rollover_from_half1 = 0.0
    if current_half == 2:
        rollover_from_half1 = sum(
            w["unused_budget"]
            for w in week_stats
            if w["half"] == 1
        )

    rollover_pool = float(rollover_same_half + rollover_from_half1)

    if current_half == 1:
        remaining_window = [w for w in week_stats if w["half"] == 1 and w["week"] >= current_week_num]
    else:
        remaining_window = [w for w in week_stats if w["half"] == 2 and w["week"] >= current_week_num]

    remaining_moments_in_window = sum(int(w["planned_moments"]) for w in remaining_window)
    if remaining_moments_in_window <= 0:
        remaining_moments_in_window = 1

    rollover_per_moment = rollover_pool / float(remaining_moments_in_window)
    base_for_today = float(current_week["base_budget_per_moment"])
    daily_budget = round(base_for_today + rollover_per_moment, 2)

    planned_half_budget = sum(
        float(w["planned_moments"]) * float(w["base_budget_per_moment"])
        for w in week_stats
        if w["half"] == current_half
    )

    if current_half == 1:
        total_flight_budget = round(planned_half_budget, 2)
    else:
        total_flight_budget = round(planned_half_budget + rollover_from_half1, 2)

    return {
        "daily_budget": daily_budget,
        "total_budget": total_flight_budget,
        "week": current_week_num,
        "half": current_half,
        "rollover_from_previous": round(rollover_pool, 2),
        "remaining_moments_in_window": int(remaining_moments_in_window),
        "base_budget_per_moment": base_for_today,
    }


# ============ MAIN LOGIC ============

def run_budget_updates_from_bq(df: pd.DataFrame):
    """Single-run logic: set campaign + adgroup daily caps, keep total flight budget unchanged."""
    required_columns = {"Date", "Trigger"}
    missing = required_columns - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns in BQ table: {sorted(missing)}")

    today_sydney = datetime.now(CAMPAIGN_TZ).date()

    working_df = df.copy()
    working_df["Date_parsed"] = pd.to_datetime(working_df["Date"], errors="coerce", dayfirst=True)
    working_df["Date_only"] = working_df["Date_parsed"].dt.date
    working_df["Trigger_norm"] = working_df["Trigger"].astype(str).str.strip().str.lower()

    filtered_df = working_df[
        (working_df["Trigger_norm"] == "spike") &
        (working_df["Date_only"] == today_sydney)
    ]
    has_spike = not filtered_df.empty

    print(f"[CF] Total rows in source: {len(df)}")
    print(f"[CF] Campaign timezone: Australia/Sydney — today is {today_sydney.isoformat()}")
    print(f"[CF] Rows matching Trigger='Spike' and Date=today: {len(filtered_df)}")

    results = []

    for campaign_id in ["81ub60c", "51tk9dq"]:
        try:
            campaign_data = get_campaign_details(campaign_id)
            selected_flight_id = select_target_flight_id(campaign_data, preferred_flight_id=None)
            current_total = get_selected_flight_total_budget(campaign_data, selected_flight_id)
            if has_spike:
                budget_info = get_rollover_budget_for_campaign(campaign_id, working_df, target_date=today_sydney)
                if budget_info is None:
                    raise ValueError(f"No weekly budget window found for {campaign_id} on {today_sydney}")

                daily_value = float(budget_info["daily_budget"])

                success = update_kokai_budget(
                    campaign_id=campaign_id,
                    flight_id=selected_flight_id,
                    daily_budget=daily_value,
                    total_budget=current_total,
                )

                adgroup_result = update_campaign_adgroup_daily_caps(campaign_id, daily_budget=daily_value)
                if adgroup_result["errors"]:
                    print(f"[CF] ⚠ {campaign_id} | adgroup cap update partial failure: {adgroup_result['errors'][:2]}")

                results.append({
                    "run_date": today_sydney,
                    "campaign_id": campaign_id,
                    "flight_id": selected_flight_id,
                    "mode": "spike",
                    "week": budget_info["week"],
                    "half": budget_info["half"],
                    "base_budget_per_moment": budget_info["base_budget_per_moment"],
                    "rollover_from_previous": budget_info["rollover_from_previous"],
                    "remaining_moments_in_window": budget_info["remaining_moments_in_window"],
                    "daily_budget_set": daily_value,
                    "total_flight_budget": current_total,
                    "updated": bool(success),
                    "updated_at_sydney": datetime.now(CAMPAIGN_TZ).isoformat(),
                    "adgroups_found": adgroup_result["count"],
                    "adgroups_updated": adgroup_result["updated"],
                    "adgroups_errors": "; ".join(adgroup_result["errors"][:3]) if adgroup_result["errors"] else None,
                    "error": None,
                })
                print(
                    f"[CF] ✓ {campaign_id} | spike mode | week {budget_info['week']} half {budget_info['half']} | "
                    f"daily={daily_value} total_kept={current_total} | "
                    f"adgroups updated {adgroup_result['updated']}/{adgroup_result['count']}"
                )
            else:
                total_value, adgroup_result = set_daily_target_zero_keep_total(
                    campaign_id,
                    selected_flight_id,
                )
                results.append({
                    "run_date": today_sydney,
                    "campaign_id": campaign_id,
                    "flight_id": selected_flight_id,
                    "mode": "no_spike_reset",
                    "week": None,
                    "half": None,
                    "base_budget_per_moment": None,
                    "rollover_from_previous": None,
                    "remaining_moments_in_window": None,
                    "daily_budget_set": 0.0,
                    "total_flight_budget": total_value,
                    "updated": True,
                    "updated_at_sydney": datetime.now(CAMPAIGN_TZ).isoformat(),
                    "adgroups_found": adgroup_result["count"],
                    "adgroups_updated": adgroup_result["updated"],
                    "adgroups_errors": "; ".join(adgroup_result["errors"][:3]) if adgroup_result["errors"] else None,
                    "error": None,
                })
                print(
                    f"[CF] ✓ {campaign_id} | no spike mode | daily=0.0 total_kept={total_value} | "
                    f"adgroups updated {adgroup_result['updated']}/{adgroup_result['count']}"
                )
            
        except Exception as err:
            results.append({
                "run_date": today_sydney,
                "campaign_id": campaign_id,
                "flight_id": None,
                "mode": "error",
                "week": None,
                "half": None,
                "base_budget_per_moment": None,
                "rollover_from_previous": None,
                "remaining_moments_in_window": None,
                "daily_budget_set": None,
                "total_flight_budget": None,
                "updated": False,
                "updated_at_sydney": datetime.now(CAMPAIGN_TZ).isoformat(),
                "adgroups_found": None,
                "adgroups_updated": None,
                "adgroups_errors": None,
                "error": str(err),
            })
            print(f"[CF] ❌ Campaign {campaign_id} update failed: {err}")

    return pd.DataFrame(results)

# ============ CLOUD FUNCTION ENTRY POINTS ============

def _execute_budget_update():
    """Core execution shared by HTTP and Eventarc entry points."""
    try:
        print("[CF] Starting TTD spike budget update...")
        
        # Load BigQuery data
        print("[CF] Loading BigQuery table...")
        query = f"SELECT Date, Trigger FROM `{FULL_TABLE_ID}` LIMIT 1000"
        bq = get_bq_client()
        query_rows = bq.query(query).result()
        updates_df = pd.DataFrame([{"Date": row.get("Date"), "Trigger": row.get("Trigger")} for row in query_rows])
        print(f"[CF] Loaded {len(updates_df)} rows from BigQuery")
        
        # Run budget update logic
        print("[CF] Running budget update logic...")
        results_df = run_budget_updates_from_bq(updates_df)
        
        print("[CF] Budget processing completed")
        updates_list = results_df.to_dict(orient='records')
        return {
            "status": "success",
            "updates": updates_list,
            "timestamp": datetime.now(CAMPAIGN_TZ).isoformat()
        }, 200
            
    except Exception as e:
        print(f"[CF] Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return {
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now(CAMPAIGN_TZ).isoformat()
        }, 500


def ttd_spike_budget_update(request: Request):
    """HTTP entry point (manual test / scheduler / direct call)."""
    return _execute_budget_update()


def ttd_spike_budget_update_event(data, context=None):
    """Event trigger entry point for BigQuery audit-log events."""
    event_id = getattr(context, "event_id", "unknown") if context is not None else "unknown"
    event_type = getattr(context, "event_type", "unknown") if context is not None else "unknown"
    print(f"[CF] Event trigger received | id={event_id} type={event_type}")

    body, status = _execute_budget_update()
    if status >= 400:
        raise RuntimeError(json.dumps(body))

    return body
