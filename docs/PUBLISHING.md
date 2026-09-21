# Publishing checklist

This project handles browser sessions, lead data, Google Sheets, and optional webhook-based research. Perform these checks before the first push and before every release.

1. Review the staged set with `git status --short` and `git diff --cached --stat`. It must not contain `state/`, `debug/`, `output/`, browser profiles, exports, credentials, `node_modules/`, or packaged `.app` files.
2. Search staged content for credentials and real operational identifiers. Rotate any value that was previously committed or shared, including service-account keys and webhook secrets.
3. Copy `.env.example` to a local `.env`; set values through your shell, secret manager, or CI secret store. Never add `.env` or a service-account JSON file to Git.
4. For the Google Apps Script client, set its Script Properties before deploying. Use an owner-only deployment unless broader access is explicitly required.
5. Run `npm test`, `npm audit`, and `npm --prefix ORCHESTRATION/monitor_app audit` before pushing. The nested research workflow currently has an unresolved upstream `xlsx` advisory; do not deploy that workflow with unreviewed dependency risk.
6. Do not expose `ORCHESTRATION/monitor_app/local_server.py` beyond loopback. It intentionally has no authentication because it is a local process-control API.


