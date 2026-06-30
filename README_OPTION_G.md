# EZRA Demo v2 — Option G: Teacher adds exercise into Google Sheet

## What changed
- The teacher dashboard still has an interactive "Add exercise" form.
- When the teacher clicks save, the backend appends a new row to the master Google Sheet catalog.
- The Google Sheet remains the single source of truth.
- The exercise is then selected immediately for that teacher.

## Required environment variable
Set `GOOGLE_CREDENTIALS_JSON` to the full service-account JSON.
The service-account email must have **Editor** permission on:
1. The exercise catalog Google Sheet (`CATALOG_SHEET_ID`)
2. The results Google Sheet (`RESULTS_SHEET_ID`), if you want result writing too.

## Optional environment variables
- `CATALOG_SHEET_ID`
- `RESULTS_SHEET_ID`
- `EZRA_APP_BASE_URL` default: `https://app.ezra.clap.co.il`

## Catalog row format
The backend appends:
- Column A: exercise name
- Column B: blank / reserved
- Column C: EZRA app URL: `...?lang=en&link=<published_csv_url>`

This matches the existing loader, which reads the exercise name from column A and extracts the CSV link from column C.


Note: the code also accepts GOOGLE_SERVICE_ACCOUNT_JSON as a fallback alias, but Render is currently configured with GOOGLE_CREDENTIALS_JSON.
