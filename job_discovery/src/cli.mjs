import fs from "node:fs/promises";
import path from "node:path";
import { platforms, roles } from "../config/defaults.mjs";
import { loadEnvironment, loadLocalSettings, settingsFromControl } from "./config.mjs";
import { buildQueries } from "./query-builder.mjs";
import { createListingReader, createSearchProvider } from "./search-provider.mjs";
import { AppsScriptSheetsClient, GoogleSheetsClient } from "./sheets.mjs";
import { StateStore } from "./state-store.mjs";
import { reverifyStoredJobs, runDiscovery } from "./run.mjs";
import { startScheduler } from "./scheduler.mjs";
import { configurationFromRows } from "./runtime-configuration.mjs";

let shutdownRequested = false;
let schedulerTimer;
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    if (shutdownRequested) process.exit(130);
    shutdownRequested = true;
    process.exitCode = 130;
    if (schedulerTimer) clearInterval(schedulerTimer);
    console.error(`${signal} received; the active query will checkpoint and finish before shutdown.`);
  });
}

async function buildRuntime() {
  const environment = await loadEnvironment();
  const stateStore = new StateStore(path.resolve(environment.STATE_FILE || "./data/state.json"));
  let sheets;
  if (environment.SHEETS_TRANSPORT === "apps-script" && environment.GOOGLE_APPS_SCRIPT_URL && environment.APPS_SCRIPT_TOKEN) {
    sheets = new AppsScriptSheetsClient({ endpoint: environment.GOOGLE_APPS_SCRIPT_URL, token: environment.APPS_SCRIPT_TOKEN });
  } else if (environment.GOOGLE_SHEET_ID && environment.GOOGLE_SERVICE_ACCOUNT_JSON) {
    const source = environment.GOOGLE_SERVICE_ACCOUNT_JSON;
    const serviceAccountJson = source.trim().startsWith("{") ? source : await fs.readFile(path.resolve(source), "utf8");
    sheets = new GoogleSheetsClient({ spreadsheetId: environment.GOOGLE_SHEET_ID, serviceAccountJson });
  }
  const searchProvider = createSearchProvider(environment);
  const listingReader = createListingReader(environment, searchProvider);
  const controlSource = (environment.CONTROL_SOURCE || "local").toLowerCase();
  const configurationSource = (environment.CONFIGURATION_SOURCE || "local").toLowerCase();
  const getSettings = async () => controlSource === "sheet" && sheets
    ? settingsFromControl(await sheets.readControl())
    : loadLocalSettings(environment.LOCAL_CONTROL_FILE || "./config/runtime.json");
  const getConfiguration = async () => configurationFromRows(configurationSource === "sheet" && sheets?.readConfiguration ? await sheets.readConfiguration() : {});
  const dependencies = { searchProvider, listingReader, sheets, shouldStop: () => shutdownRequested };
  const getRunDependencies = async () => {
    const configuration = await getConfiguration();
    return { ...dependencies, queryInventory: configuration.queries, signalRules: configuration.signalRules };
  };
  const getRunContext = async () => {
    const [settings, configuration] = await Promise.all([getSettings(), getConfiguration()]);
    return { settings, configuration, dependencies: { ...dependencies, queryInventory: configuration.queries, signalRules: configuration.signalRules } };
  };
  return { environment, stateStore, sheets, controlSource, configurationSource, getSettings, getConfiguration, getRunDependencies, getRunContext, dependencies };
}

async function main() {
  const command = process.argv[2] || "help";
  const runtime = await buildRuntime();
  if (command === "setup-sheet") {
    if (!runtime.sheets) throw new Error("Configure Apps Script URL/token or direct Google Sheets credentials before setup-sheet.");
    const localFirst = runtime.controlSource === "local" && runtime.configurationSource === "local";
    await runtime.sheets.setup({ platforms, roles, queries: buildQueries(), localFirst });
    console.log(localFirst
      ? "Google Sheet output tabs and priority views are ready; local-only configuration tabs were removed."
      : "Google Sheet tabs, configuration, query inventory, and headers are ready.");
    return;
  }
  if (command === "reset") {
    if (!runtime.sheets?.clearProjection) throw new Error("The Apps Script Sheet transport is required to clear the live Sheet projection.");
    const resetRunId = `reset_${Date.now()}`;
    if (!await runtime.stateStore.acquireRunLock(resetRunId)) throw new Error("A discovery run is active; stop or let it finish before resetting state.");
    try {
      const [database, projection] = await Promise.all([runtime.stateStore.reset(), runtime.sheets.clearProjection()]);
      console.log(JSON.stringify({ reset: true, database, projection }, null, 2));
    } finally {
      await runtime.stateStore.releaseRunLock();
    }
    return;
  }
  if (command === "run") {
    const context = await runtime.getRunContext();
    const run = await runDiscovery({ ...context.dependencies, stateStore: runtime.stateStore, settings: context.settings, trigger: "manual" });
    console.log(JSON.stringify(run, null, 2));
    return;
  }
  if (command === "reverify") {
    const configuration = await runtime.getConfiguration();
    const result = await reverifyStoredJobs({ stateStore: runtime.stateStore, sheets: runtime.sheets, signalRules: configuration.signalRules });
    console.log(JSON.stringify(result, null, 2));
    return;
  }
  if (command === "smoke-test") {
    const smokeQuery = { id: "smoke-test", platform: "Ashby", role: "Python Developer", type: "smoke-test", query: "synthetic smoke test", allowedHosts: ["jobs.ashbyhq.com"] };
    const searchProvider = { search: async () => [{ title: "Junior Python Developer — Test Record", link: "https://jobs.ashbyhq.com/daily-job-discovery/test-job-0001", snippet: "Remote role requiring Python." }] };
    const listingReader = async () => ({ title: "Junior Python Developer — Test Record", description: "This is a system smoke test. The role is fully remote and requires Python.", company: "Daily Job Discovery Test", location: "Remote", canonicalUrl: "https://jobs.ashbyhq.com/daily-job-discovery/test-job-0001" });
    const settings = { ...await runtime.getSettings(), maxQueriesPerRun: 1, maxListingsPerRun: 1, minListingDelayMs: 0, maxListingDelayMs: 0, minQueryDelayMs: 0, maxQueryDelayMs: 0, cooldownMinMs: 0, cooldownMaxMs: 0 };
    const run = await runDiscovery({ ...runtime.dependencies, searchProvider, listingReader, stateStore: runtime.stateStore, settings, queryInventory: [smokeQuery], queries: [smokeQuery], trigger: "smoke-test", testMode: true, forceHydration: true });
    console.log(JSON.stringify(run, null, 2));
    return;
  }
  if (command === "live-test") {
    if (runtime.environment.SEARCH_PROVIDER !== "playwright-google") throw new Error("Set SEARCH_PROVIDER=playwright-google before running a live test.");
    const [state, configuration] = await Promise.all([runtime.stateStore.read(), runtime.getConfiguration()]);
    const query = configuration.queries.find((item) => (state.liveTestUsage?.[item.id] || 0) < 2);
    if (!query) throw new Error("Every configured query has reached the two-use live-test limit.");
    runtime.dependencies.searchProvider.maxResults = 1;
    const settings = { ...await runtime.getSettings(), maxQueriesPerRun: 1, maxListingsPerRun: 1 };
    const run = await runDiscovery({ ...runtime.dependencies, signalRules: configuration.signalRules, stateStore: runtime.stateStore, settings, queries: [query], trigger: "live-e2e" });
    if (run.queriesAttempted) {
      const updated = await runtime.stateStore.read();
      updated.liveTestUsage = { ...(updated.liveTestUsage || {}), [query.id]: (updated.liveTestUsage?.[query.id] || 0) + 1 };
      await runtime.stateStore.write(updated);
    }
    console.log(JSON.stringify(run, null, 2));
    return;
  }
  if (command === "schedule") {
    console.log(`Scheduler started. It checks ${runtime.controlSource} control once per minute; Ctrl+C stops it.`);
    schedulerTimer = startScheduler({ getSettings: runtime.getSettings, getDependencies: runtime.getRunDependencies, stateStore: runtime.stateStore, dependencies: runtime.dependencies });
    return;
  }
  if (command === "status") {
    const [context, state, database] = await Promise.all([runtime.getRunContext(), runtime.stateStore.read(), runtime.stateStore.stats()]);
    const jobs = Object.values(state.jobs || {});
    const runs = Object.values(state.runs || {})
      .sort((left, right) => String(right.startedAt || "").localeCompare(String(left.startedAt || "")))
      .slice(0, 12)
      .map((run) => ({
        id: run.id,
        trigger: run.trigger,
        status: run.status,
        startedAt: run.startedAt,
        endedAt: run.endedAt,
        queriesAttempted: run.queriesAttempted || 0,
        queriesCompleted: run.queriesCompleted || 0,
        hydrated: run.hydrated || 0,
        newJobs: run.newJobs || 0,
        errors: (run.errors || []).slice(0, 3),
        stopReason: run.stopReason || ""
      }));
    const countBy = (values, key) => Object.fromEntries(
      Object.entries(values.reduce((counts, item) => {
        const value = String(item[key] || "unknown");
        counts[value] = (counts[value] || 0) + 1;
        return counts;
      }, {})).sort(([left], [right]) => left.localeCompare(right))
    );
    const queryProgress = Object.values(state.queryProgress || {});
    console.log(JSON.stringify({
      controlSource: runtime.controlSource, configurationSource: runtime.configurationSource, automationEnabled: context.settings.automationEnabled, dailyRunTime: context.settings.dailyRunTime, timezone: context.settings.timezone,
      enabledQueries: context.configuration.queries.length, queryCursor: state.queryCursor, queryCursorId: state.queryCursorId,
      activeRunId: state.activeRunId, lastScheduledDate: state.lastScheduledDate, database,
      jobStatusCounts: countBy(jobs, "status"),
      queryProgressCounts: countBy(queryProgress, "status"),
      recentRuns: runs
    }, null, 2));
    return;
  }
  console.log("Commands: setup-sheet | reset | run | reverify | smoke-test | live-test | schedule | status");
}

main().then(() => {
  // The scheduler is intentionally persistent; one-off commands must release
  // the Playwright CLI connection once their work has been awaited.
  if (process.argv[2] !== "schedule") process.exit(process.exitCode || 0);
}).catch((error) => { console.error(error.message); process.exitCode = 1; });
