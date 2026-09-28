import fs from "node:fs/promises";
import path from "node:path";
import { defaults } from "../config/defaults.mjs";

export async function loadEnvironment(file = ".env") {
  const environment = { ...process.env };
  try {
    const source = await fs.readFile(path.resolve(file), "utf8");
    for (const line of source.split(/\r?\n/)) {
      const match = line.match(/^\s*([A-Z0-9_]+)=(.*)\s*$/);
      if (match && environment[match[1]] === undefined) environment[match[1]] = match[2].replace(/^['"]|['"]$/g, "");
    }
  } catch (error) { if (error.code !== "ENOENT") throw error; }
  return environment;
}

function normalizeSettings(input) {
  const settings = { ...defaults, ...input };
  const pairs = [
    ["minListingDelayMs", "maxListingDelayMs"], ["minListingDwellMs", "maxListingDwellMs"], ["minPageDelayMs", "maxPageDelayMs"],
    ["minSearchPageCooldownMs", "maxSearchPageCooldownMs"],
    ["minQueryDelayMs", "maxQueryDelayMs"], ["cooldownMinMs", "cooldownMaxMs"]
  ];
  for (const [minimum, maximum] of pairs) {
    settings[minimum] = Math.max(0, Number(settings[minimum]) || 0);
    settings[maximum] = Math.max(settings[minimum], Number(settings[maximum]) || 0);
  }
  for (const key of ["maxQueriesPerRun", "maxListingsPerRun", "searchPageBurstSize", "queryBurstSize", "maxSearchMinutesPerQuery", "searchRetryAttempts", "listingRetryAttempts", "staleLockMinutes", "closeAfterMisses"]) settings[key] = Math.max(1, Math.floor(Number(settings[key]) || defaults[key]));
  settings.maxPagesPerQuery = Math.max(0, Math.floor(Number(settings.maxPagesPerQuery) || 0));
  if (!/^\d{2}:\d{2}$/.test(settings.dailyRunTime)) throw new Error("dailyRunTime must use HH:MM format");
  new Intl.DateTimeFormat("en", { timeZone: settings.timezone }).format();
  return settings;
}

export async function loadLocalSettings(file = "./config/runtime.json") {
  const source = JSON.parse(await fs.readFile(path.resolve(file), "utf8"));
  return normalizeSettings(source);
}

export function settingsFromControl(control = {}) {
  const number = (key, fallback, legacyKey) => {
    const raw = control[key] || (legacyKey ? control[legacyKey] : undefined);
    const parsed = Number(raw ?? fallback);
    return Number.isFinite(parsed) ? parsed : fallback;
  };
  const settings = {
    ...defaults,
    automationEnabled: /^true$/i.test(control["Automation Enabled"] ?? String(defaults.automationEnabled)),
    dailyRunTime: control["Daily Run Time"] || defaults.dailyRunTime,
    timezone: control.Timezone || defaults.timezone,
    maxQueriesPerRun: number("Max Queries Per Run", defaults.maxQueriesPerRun),
    maxListingsPerRun: number("Max Listings Per Run", defaults.maxListingsPerRun),
    minListingDelayMs: number("Minimum Listing Delay (ms)", defaults.minListingDelayMs, "Minimum Delay (ms)"),
    maxListingDelayMs: number("Maximum Listing Delay (ms)", defaults.maxListingDelayMs, "Maximum Delay (ms)"),
    minListingDwellMs: number("Minimum Listing Dwell (ms)", defaults.minListingDwellMs),
    maxListingDwellMs: number("Maximum Listing Dwell (ms)", defaults.maxListingDwellMs),
    minPageDelayMs: number("Minimum Page Delay (ms)", defaults.minPageDelayMs),
    maxPageDelayMs: number("Maximum Page Delay (ms)", defaults.maxPageDelayMs),
    minSearchPageCooldownMs: number("Minimum Search Page Cooldown (ms)", defaults.minSearchPageCooldownMs),
    maxSearchPageCooldownMs: number("Maximum Search Page Cooldown (ms)", defaults.maxSearchPageCooldownMs),
    searchPageBurstSize: number("Search Page Burst Size", defaults.searchPageBurstSize),
    minQueryDelayMs: number("Minimum Inter-query Delay (ms)", defaults.minQueryDelayMs),
    maxQueryDelayMs: number("Maximum Inter-query Delay (ms)", defaults.maxQueryDelayMs),
    queryBurstSize: number("Query Burst Size", control["Batch Size"] || defaults.queryBurstSize),
    cooldownMinMs: number("Minimum Cooldown (ms)", control["Batch Pause (ms)"] || defaults.cooldownMinMs),
    cooldownMaxMs: number("Maximum Cooldown (ms)", control["Batch Pause (ms)"] || defaults.cooldownMaxMs),
    maxPagesPerQuery: number("Maximum Pages Per Query", defaults.maxPagesPerQuery),
    maxSearchMinutesPerQuery: number("Maximum Search Minutes Per Query", defaults.maxSearchMinutesPerQuery),
    searchRetryAttempts: number("Search Retry Attempts", defaults.searchRetryAttempts),
    listingRetryAttempts: number("Listing Retry Attempts", defaults.listingRetryAttempts),
    retryBaseDelayMs: number("Retry Base Delay (ms)", defaults.retryBaseDelayMs),
    recheckAfterDays: number("Verified Job Recheck Days", defaults.recheckAfterDays),
    staleLockMinutes: number("Stale Lock Minutes", defaults.staleLockMinutes),
    closeAfterMisses: number("Close After Query Misses", defaults.closeAfterMisses)
  };
  return normalizeSettings(settings);
}
