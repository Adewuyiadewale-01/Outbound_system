# Google Apps Script properties

The web app reads operational identifiers and webhook configuration from Google Apps Script **Script Properties**, not from source code. In the Apps Script editor, open **Project Settings → Script Properties** and configure the values below for your private deployment.

| Property | Required | Purpose |
| --- | --- | --- |
| `LEADS_SPREADSHEET_ID` | Yes | Primary lead-review spreadsheet ID |
| `COMPARISON_SPREADSHEET_ID` | Yes | Spreadsheet used for name comparison |
| `COMPARISON_SHEET_GID` | Yes | Comparison tab GID |
| `OBF_SPREADSHEET_ID` | Yes | Outreach/reporting spreadsheet ID |
| `DOC_WEBHOOK_URL` | Optional | Endpoint used to open deployed research documents |
| `DOC_WEBHOOK_SECRET` | Optional | Shared secret sent to the webhook; use a long random value |

Do not add the values to `Code.gs`, commit them, or share them in logs. Deploy the web app with the narrowest access setting that satisfies the intended users.
