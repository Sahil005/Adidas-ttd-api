# TTD Spike Budget Update

Cloud Function automation for TTD campaign budget updates using BigQuery Spike signals.

## What this project does
- Reads `Date` and `Trigger` from BigQuery table `acceleration-australia.adidas_ttd.sb_campaign_budget_updates`
- Applies budget logic for campaigns:
  - `81ub60c`
  - `51tk9dq`
- Calls TTD GraphQL API to update:
  - Daily budget (`dailyTargetInAdvertiserCurrency`)
  - Total flight budget (`budgetInAdvertiserCurrency`)

## Budget model
Budget configuration is code-driven in `CAMPAIGN_WEEKLY_PLAN` (in `main.py`), with week-level:
- `week_start`
- `week_end`
- `half`
- `planned_moments`
- `base_budget_per_moment`

Rollover behavior:
- Unused weekly budget rolls within the same half
- Remaining Half 1 budget can roll into Half 2 calculations (as defined in current logic)

## Run behavior (current)

### Spike exists for run date
- Compute daily + total via `get_rollover_budget_for_campaign(...)`
- Update both values in TTD

### No spike for run date
- Set daily budget to `0.0`
- Align total budget:
  - Inside planned window: align to computed period total
  - Outside planned window: align to Half 1 baseline total via `get_campaign_baseline_total_budget(...)`

## Scheduling
Production scheduler is set to:
- `0 10 * * *`
- Time zone: `Australia/Sydney`

So the function runs every day at 10:00 AM Sydney time.

## Local notebook validation
Notebook: `TTD API V3.ipynb`

Use it to test:
- Spike-day behavior
- No-spike behavior
- Outside-window fallback behavior

## Deploy
See full instructions in `DEPLOYMENT.md`.

Quick deploy command:

```cmd
gcloud config set project acceleration-australia

gcloud functions deploy ttd-spike-budget-update-http --gen2 --runtime python311 --region australia-southeast1 --source . --entry-point ttd_spike_budget_update --trigger-http --allow-unauthenticated --set-env-vars PROJECT_ID=acceleration-australia,DATASET_ID=adidas_ttd,TABLE_ID=sb_campaign_budget_updates,SECRET_ID=ttd-s --timeout 300s --memory 512Mi
```
