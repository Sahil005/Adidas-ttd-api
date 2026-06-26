# TTD Spike Budget Update - Deployment Guide

## Overview
This Cloud Function automates TTD campaign budget updates from BigQuery Spike triggers.

It now uses:
- Weekly plan configuration from `CAMPAIGN_WEEKLY_PLAN` in `main.py`
- Daily scheduler at **10:00 AM Australia/Sydney**
- No-spike behavior that sets daily budget to `0.0` and aligns total budget

## Current runtime behavior

### 1) Spike found for run date
- Computes budget from weekly plan + rollover logic
- Updates for each campaign:
  - `dailyTargetInAdvertiserCurrency` = computed daily budget
  - `budgetInAdvertiserCurrency` = computed total budget

### 2) No spike for run date
- Sets daily budget to `0.0`
- Total budget handling:
  - If run date is inside a configured week/half window: aligns to that period’s computed total
  - If run date is outside planned windows: aligns to campaign Half 1 baseline total

### 3) Outside planned window + spike
- Returns error for that campaign (`No weekly budget window found ...`)
- Does not apply spike budget for that campaign

## Current campaign plan in code (`main.py`)

### Campaign `81ub60c`
- Week 1: 2026-06-17 to 2026-06-23, half 1, moments 3, base 798.64
- Week 2: 2026-06-24 to 2026-06-30, half 1, moments 3, base 798.64
- Week 3: 2026-07-01 to 2026-07-07, half 1, moments 3, base 798.64
- Week 4: 2026-07-08 to 2026-07-14, half 2, moments 2, base 2695.42
- Week 5: 2026-07-15 to 2026-07-21, half 2, moments 2, base 2695.42

Half totals:
- Half 1 baseline total: `7187.76`
- Half 2 planned total: `10781.68`

### Campaign `51tk9dq`
- Week 1: 2026-06-17 to 2026-06-23, half 1, moments 3, base 1958.22
- Week 2: 2026-06-24 to 2026-06-30, half 1, moments 3, base 1958.22
- Week 3: 2026-07-01 to 2026-07-07, half 1, moments 3, base 1958.22
- Week 4: 2026-07-08 to 2026-07-14, half 2, moments 2, base 3604.9
- Week 5: 2026-07-15 to 2026-07-21, half 2, moments 2, base 3604.9

Half totals:
- Half 1 baseline total: `17623.98`
- Half 2 planned total: `14419.60`

## Deploy function (cmd)
Run from project folder:

```cmd
gcloud config set project acceleration-australia

gcloud functions deploy ttd-spike-budget-update-http --gen2 --runtime python311 --region australia-southeast1 --source . --entry-point ttd_spike_budget_update --trigger-http --allow-unauthenticated --set-env-vars PROJECT_ID=acceleration-australia,DATASET_ID=adidas_ttd,TABLE_ID=sb_campaign_budget_updates,SECRET_ID=ttd-s --timeout 300s --memory 512Mi
```

## Scheduler configuration (current)
Current job:
- Job name: `ttd-spike-budget-poller`
- Region: `australia-southeast1`
- Schedule: `0 10 * * *`
- Timezone: `Australia/Sydney`
- Target: `ttd-spike-budget-update-http`

Update scheduler (cmd):

```cmd
gcloud config set project acceleration-australia

gcloud scheduler jobs update http ttd-spike-budget-poller --location australia-southeast1 --schedule "0 10 * * *" --time-zone Australia/Sydney
```

Verify scheduler:

```cmd
gcloud scheduler jobs describe ttd-spike-budget-poller --location australia-southeast1 --format="yaml(schedule,timeZone,state,httpTarget.uri)"
```

## Manual run + logs

Run scheduler immediately:

```cmd
gcloud scheduler jobs run ttd-spike-budget-poller --location australia-southeast1
```

Read logs:

```cmd
gcloud functions logs read ttd-spike-budget-update-http --gen2 --region australia-southeast1 --limit 100
```

## Expected log examples

No spike inside planned window:
- `... no spike mode | week X half Y -> daily=0.0 total aligned=...`

No spike outside planned window:
- `... no spike mode | outside planned window -> daily=0.0 total aligned=...`

Spike inside planned window:
- `... spike mode | week X half Y | ... -> daily=... total=...`
