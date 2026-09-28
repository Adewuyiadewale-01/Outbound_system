import crypto from "node:crypto";

const tabSchemas = {
  "Control": ["Setting", "Value", "Description"],
  "ATS Platforms": ["Platform", "Google site target", "Enabled"],
  "Roles & Vocabulary": ["Role", "Search type", "Vocabulary"],
  "Queries": ["Query ID", "Platform", "Role", "Search type", "Google query", "Enabled"],
  "Jobs": ["job_id", "company_id", "company", "title", "location", "application_url", "ats_platform", "role_match", "junior_status", "remote_status", "python_status", "evidence_text", "source_queries", "first_seen_at", "last_seen_at", "verification_checked_at", "score", "status", "review_reason"],
  "Companies": ["company_id", "company_name", "company_domain", "ats_platforms", "career_urls", "first_seen_at", "last_seen_at", "active_job_count", "remote_hiring_signal", "status", "notes"],
  "Runs": ["run_id", "trigger", "status", "started_at", "ended_at", "queries_attempted", "results_found", "unique_candidates", "hydrated", "new_jobs", "errors", "notes"],
  "Review Queue": ["job_id", "company", "title", "application_url", "review_reason", "junior_status", "remote_status", "python_status", "checked_at"],
  "Rules": ["Rule", "Value", "Version"]
};

const defaultControl = [
  ["Automation Enabled", "FALSE", "Set TRUE to allow the scheduler to launch the daily run."],
  ["Daily Run Time", "08:00", "24-hour local time."],
  ["Timezone", "Africa/Lagos", "IANA timezone used by the scheduler."],
  ["Max Queries Per Run", "180", "Use a smaller number for testing."],
  ["Max Listings Per Run", "180", "Daily verification target. The bot finishes its current query before stopping, so this can be exceeded slightly."],
  ["Minimum Listing Delay (ms)", "8000", "Minimum spacing after a job listing read."],
  ["Maximum Listing Delay (ms)", "15000", "Maximum spacing after a job listing read."],
  ["Minimum Listing Dwell (ms)", "4000", "Minimum time spent loading and reading a job listing."],
  ["Maximum Listing Dwell (ms)", "7000", "Maximum time spent loading and reading a job listing."],
  ["Minimum Page Delay (ms)", "15000", "Minimum spacing before advancing a Google results page."],
  ["Maximum Page Delay (ms)", "30000", "Maximum spacing before advancing a Google results page."],
  ["Search Page Burst Size", "3", "Pause after this many consecutive Google results pages within one query."],
  ["Minimum Search Page Cooldown (ms)", "180000", "Shortest pause between result-page bursts (3 minutes)."],
  ["Maximum Search Page Cooldown (ms)", "300000", "Longest pause between result-page bursts (5 minutes)."],
  ["Minimum Inter-query Delay (ms)", "90000", "Minimum spacing between completed Google queries."],
  ["Maximum Inter-query Delay (ms)", "180000", "Maximum spacing between completed Google queries."],
  ["Query Burst Size", "3", "Complete this many full queries before a longer cooldown."],
  ["Minimum Cooldown (ms)", "600000", "Shortest pause between query bursts (10 minutes)."],
  ["Maximum Cooldown (ms)", "900000", "Longest pause between query bursts (15 minutes)."],
  ["Maximum Pages Per Query", "0", "Optional total-page ceiling. Use 0 to paginate until Google ends or two thin pages occur."],
  ["Maximum Search Minutes Per Query", "75", "Safety time limit for a single Google query; normal pagination still stops after two low-yield pages."],
  ["Search Retry Attempts", "2", "Attempts before leaving a query pending for a later run."],
  ["Listing Retry Attempts", "3", "Attempts before leaving a failed listing pending for a later run."],
  ["Retry Base Delay (ms)", "2000", "Base delay used for bounded exponential retries."],
  ["Verified Job Recheck Days", "7", "Re-open unchanged verified jobs after this many days."],
  ["Stale Lock Minutes", "360", "Recover a lock only after this age or when its local process is gone."],
  ["Close After Query Misses", "3", "Mark a job closed after this many completed source-query misses."]
];
const defaultRules = [
  ["Rules version", "3", "3"],
  ["Junior signals", "junior | jr | associate | entry level | new grad | graduate | I | 1 | early career", "2"],
  ["Senior signals", "senior | staff | principal | lead | manager | director", "2"],
  ["Remote signals", "remote | work from home | distributed | anywhere", "2"],
  ["Hybrid signals", "hybrid", "3"],
  ["Onsite signals", "no remote | on-site | onsite | in office", "3"],
  ["Non-remote signals", "no remote | on-site | onsite | in office | hybrid only", "2"],
  ["Python signals", "python", "2"]
];

const b64url = (value) => Buffer.from(value).toString("base64url");
const nowEpoch = () => Math.floor(Date.now() / 1000);

export class GoogleSheetsClient {
  constructor({ spreadsheetId, serviceAccountJson, fetchImpl = fetch }) {
    this.spreadsheetId = spreadsheetId;
    this.serviceAccount = typeof serviceAccountJson === "string" ? JSON.parse(serviceAccountJson) : serviceAccountJson;
    this.fetchImpl = fetchImpl;
    this.token = undefined;
  }

  async accessToken() {
    if (this.token?.expiresAt > Date.now() + 60_000) return this.token.value;
    const assertionHeader = b64url(JSON.stringify({ alg: "RS256", typ: "JWT" }));
    const assertionPayload = b64url(JSON.stringify({ iss: this.serviceAccount.client_email, scope: "https://www.googleapis.com/auth/spreadsheets", aud: "https://oauth2.googleapis.com/token", iat: nowEpoch(), exp: nowEpoch() + 3600 }));
    const signer = crypto.createSign("RSA-SHA256"); signer.update(`${assertionHeader}.${assertionPayload}`); signer.end();
    const assertion = `${assertionHeader}.${assertionPayload}.${signer.sign(this.serviceAccount.private_key, "base64url")}`;
    const response = await this.fetchImpl("https://oauth2.googleapis.com/token", { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer", assertion }) });
    if (!response.ok) throw new Error(`Could not authenticate to Google Sheets: HTTP ${response.status}`);
    const body = await response.json(); this.token = { value: body.access_token, expiresAt: Date.now() + body.expires_in * 1000 }; return this.token.value;
  }

  async request(path, options = {}) {
    const token = await this.accessToken();
    const response = await this.fetchImpl(`https://sheets.googleapis.com/v4/spreadsheets/${this.spreadsheetId}${path}`, { ...options, headers: { authorization: `Bearer ${token}`, "content-type": "application/json", ...options.headers } });
    if (!response.ok) throw new Error(`Google Sheets request failed: HTTP ${response.status} ${await response.text()}`);
    return response.status === 204 ? undefined : response.json();
  }

  async setup({ platforms, roles, queries, localFirst = false }) {
    const existing = await this.request("");
    const existingNames = new Set(existing.sheets.map((sheet) => sheet.properties.title));
    const localOnlyTabs = new Set(["Control", "ATS Platforms", "Roles & Vocabulary", "Queries", "Rules"]);
    const desiredTabs = Object.keys(tabSchemas).filter((name) => !localFirst || !localOnlyTabs.has(name));
    const created = desiredTabs.filter((name) => !existingNames.has(name));
    const requests = created.map((title) => ({ addSheet: { properties: { title } } }));
    if (requests.length) await this.request(":batchUpdate", { method: "POST", body: JSON.stringify({ requests }) });
    const values = {};
    for (const tab of created) values[`'${tab}'!A1`] = [tabSchemas[tab]];
    if (!localFirst) {
      if (created.includes("Control")) values["'Control'!A2"] = defaultControl;
      if (created.includes("ATS Platforms")) values["'ATS Platforms'!A2"] = platforms.map((item) => [item.name, item.siteTarget, item.enabled]);
      if (created.includes("Roles & Vocabulary")) values["'Roles & Vocabulary'!A2"] = roles.flatMap((role) => [[role.name, "junior", role.junior.join(" | ")], [role.name, "unfiltered", role.unfiltered.join(" | ")]]);
      if (created.includes("Queries")) values["'Queries'!A2"] = queries.map((item) => [item.id, item.platform, item.role, item.type, item.query, true]);
      if (created.includes("Rules")) values["'Rules'!A2"] = defaultRules;
    }
    if (Object.keys(values).length) await this.request("/values:batchUpdate?valueInputOption=USER_ENTERED", { method: "POST", body: JSON.stringify({ data: Object.entries(values).map(([range, rows]) => ({ range, majorDimension: "ROWS", values: rows })) }) });
  }

  async readControl() {
    const response = await this.request("/values/'Control'!A:B");
    return Object.fromEntries((response.values || []).slice(1).filter((row) => row[0]).map((row) => [row[0], row[1] || ""]));
  }

  async readRows(tab, lastColumn) {
    const response = await this.request(`/values/${encodeURIComponent(`'${tab}'!A:${lastColumn}`)}`);
    return (response.values || []).slice(1);
  }

  async readConfiguration() {
    const [platformRows, roleRows, queryRows, ruleRows] = await Promise.all([
      this.readRows("ATS Platforms", "C"), this.readRows("Roles & Vocabulary", "C"),
      this.readRows("Queries", "F"), this.readRows("Rules", "C")
    ]);
    return { platformRows, roleRows, queryRows, ruleRows };
  }

  async append(tab, rows) {
    if (!rows.length) return;
    return this.request(`/values/${encodeURIComponent(`'${tab}'!A1`)}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS`, { method: "POST", body: JSON.stringify({ values: rows }) });
  }

  async upsert(tab, records) {
    if (!records.length) return;
    const current = await this.request(`/values/${encodeURIComponent(`'${tab}'!A:A`)}`);
    const index = new Map((current.values || []).slice(1).map((row, offset) => [row[0], offset + 2]).filter(([id]) => id));
    const updates = records.filter((record) => index.has(record.id)).map((record) => ({ range: `'${tab}'!A${index.get(record.id)}`, majorDimension: "ROWS", values: [record.values] }));
    const additions = records.filter((record) => !index.has(record.id));
    if (updates.length) await this.request("/values:batchUpdate?valueInputOption=RAW", { method: "POST", body: JSON.stringify({ data: updates }) });
    if (additions.length) await this.append(tab, additions.map((record) => record.values));
  }

  async replace(tab, rows) {
    const metadata = await this.request(`/values/${encodeURIComponent(`'${tab}'!A:ZZ`)}`);
    const rowCount = Math.max(1, (metadata.values || []).length);
    if (rowCount > 1) await this.request(`/values/${encodeURIComponent(`'${tab}'!A2:ZZ${rowCount}`)}:clear`, { method: "POST", body: "{}" });
    if (rows.length) await this.append(tab, rows);
  }
}

export class AppsScriptSheetsClient {
  constructor({ endpoint, token, fetchImpl = fetch }) { this.endpoint = endpoint; this.token = token; this.fetchImpl = fetchImpl; }

  async call(action, payload = {}) {
    const response = await this.fetchImpl(this.endpoint, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ action, token: this.token, ...payload }), redirect: "follow" });
    if (!response.ok) throw new Error(`Apps Script endpoint failed: HTTP ${response.status}`);
    const body = await response.json();
    if (!body.ok) throw new Error(body.error || "Apps Script endpoint rejected the request");
    return body;
  }

  async setup({ platforms, roles, queries, localFirst = false }) {
    return this.call("bootstrap", { platforms, roles, queries, localFirst, tabSchemas, defaultControl });
  }

  async readControl() { return (await this.call("getControl")).control; }

  async readConfiguration() {
    try { return (await this.call("getConfiguration")).configuration; }
    catch (error) { if (/unsupported action/i.test(error.message)) return {}; throw error; }
  }

  async append(tab, rows) { return this.call("append", { tab, rows }); }

  async upsert(tab, records) { return this.call("upsert", { tab, records }); }

  async replace(tab, rows) {
    try { return await this.call("replace", { tab, rows }); }
    catch (error) {
      if (!/unsupported action/i.test(error.message)) throw error;
      return rows.length ? this.upsert(tab, rows.map((values) => ({ id: values[0], values }))) : undefined;
    }
  }

  async retain(tab, ids) {
    try { return await this.call("retain", { tab, ids }); }
    catch (error) { if (/unsupported action/i.test(error.message)) return undefined; throw error; }
  }

  async syncProjection(payload) {
    try { return { supported: true, ...(await this.call("syncProjection", payload)) }; }
    catch (error) { if (/unsupported action/i.test(error.message)) return { supported: false }; throw error; }
  }

  async clearProjection() {
    for (const tab of ["Jobs", "Companies", "Runs", "Review Queue"]) await this.replace(tab, []);
    return this.syncProjection({ jobIds: [], companyIds: [], jobs: [], companies: [], reviews: [], runs: [] });
  }
}

export function jobRow(job) {
  return [job.jobId, job.companyId, job.company, job.title, job.location, job.canonicalUrl, job.platform, job.role, job.juniorStatus, job.remoteStatus, job.pythonStatus, job.evidenceText, job.sourceQueries.join(" | "), job.firstSeenAt, job.lastSeenAt, job.verificationCheckedAt, job.score, job.status, job.reviewReason];
}

export function companyRow(company) {
  // The Notes column belongs to the user, so synchronisation deliberately leaves it untouched.
  return [company.companyId, company.companyName, company.companyDomain, [...company.platforms].join(" | "), [...company.careerUrls].join(" | "), company.firstSeenAt, company.lastSeenAt, company.activeJobCount, company.remoteHiringSignal, company.status];
}

export function runRow(run) {
  const notes = [run.stopReason ? `stop=${run.stopReason}` : "", ...(run.errors || [])].filter(Boolean).join(" | ");
  return [run.id, run.trigger, run.status, run.startedAt, run.endedAt || "", run.queriesAttempted, run.resultsFound, run.uniqueCandidates, run.hydrated, run.newJobs, run.errors.length, notes];
}
