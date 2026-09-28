# Mobile Lead Activity App

Custom Google Apps Script frontend for manually reviewing `Pre-final` leads, writing ranked rows into `Final`, and finishing approved rows into the OBF `Prospects` tab.

## Files
- `Code.gs`: server-side sheet reads/writes.
- `Index.html`: mobile-first shell for lead activity and lead review.
- `LeadReview.html`: standalone review page. The deployed app serves this file for `?view=review`.

## Deploy
1. Open the lead research Google Sheet.
2. Go to `Extensions -> Apps Script`.
3. Add/replace `Code.gs` with this folder's `Code.gs`.
4. Add an HTML file named `Index` and paste `Index.html`.
5. Deploy with `Deploy -> New deployment -> Web app`.
6. Set access to your account only unless you intentionally want broader access.
7. Open the web app URL on iPhone Safari, then use `Share -> Add to Home Screen`.

## Workflow
1. App reads every pending lead from `Pre-final`.
2. Tap `Sanitize` first. It applies the same P1 archive check as `swap_prefinal_p1_from_name_sheet.py`: swap P1/P2 when P1 is blocked and P2 exists, or move the row to `Copy of Pre-final` when no P2 exists.
3. Firm name opens the company website, and LinkedIn buttons open each person profile.
4. Choose `P1 Activity` and `P2 Activity`.
5. Select `Final`, then tap `Bridge`. The app applies the P1/P2 activity swap and category rules, writes/upserts rows into `Final`, and sorts by `Hyper`, `High`, `Alpha-medium`, `Medium`, then `Low`.
6. Select `Prospects`, then tap `Finish`. The app copies non-duplicate `Final` rows into the Operation Brute Force `Prospects` tab and records the first appended row in `Outreach Control -> Prospects Start Row` for today's outreach prep.

Manual activity values saved through this app are authoritative for scoring. The LinkedIn outreach runner may still read activity for humanization and logs, but it only fills `P1 Activity` / `P2 Activity` when the destination cell is blank.

The app gates the daily flow as `Sanitize -> Bridge to final -> Bridge to prospects`.
