import { buildQueries } from "./query-builder.mjs";
import { dedupeCandidates, toCandidate } from "./dedupe.mjs";
import { readListing } from "./listing-reader.mjs";
import { verifySignals } from "./signals.mjs";
import { normalizedText, stableHash } from "./url.mjs";
import { isCareerLandingPageUrl, isJobPostingCandidate } from "./job-posting-url.mjs";
import { companyRow, jobRow, runRow } from "./sheets.mjs";
import { defaults } from "../config/defaults.mjs";

const isoNow = () => new Date().toISOString();
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const randomBetween = (minimum, maximum) => minimum + Math.floor(Math.random() * Math.max(1, maximum - minimum + 1));
const asArrayWithStop = (values, paginationStop) => Object.assign([...values], { paginationStop });
const discoveryField = (value) => value === "design" ? "design" : "engineering";
function dateKeyInTimezone(value, timezone) {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(value);
  const fields = Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return `${fields.year}-${fields.month}-${fields.day}`;
}

function completedHydrationsToday(state, { timezone, excludingRunId }) {
  const today = dateKeyInTimezone(new Date(), timezone);
  return Object.values(state.runs).reduce((totals, priorRun) => {
    if (priorRun.id === excludingRunId || priorRun.trigger === "smoke-test" || !priorRun.startedAt || dateKeyInTimezone(new Date(priorRun.startedAt), timezone) !== today) return totals;
    const byField = priorRun.hydratedByField || { engineering: Number(priorRun.hydrated) || 0 };
    for (const [field, value] of Object.entries(byField)) totals.byField[discoveryField(field)] += Number(value) || 0;
    totals.total += Number(priorRun.hydrated) || 0;
    return totals;
  }, { total: 0, byField: { engineering: 0, design: 0 } });
}

function fieldQuota(settings, field) {
  const half = Math.floor(settings.maxListingsPerRun / 2);
  return discoveryField(field) === "design" ? half : settings.maxListingsPerRun - half;
}

function mergeSources(previous = [], current = []) { return [...new Set([...previous, ...current])]; }
function companyIdentity(company, canonicalUrl) {
  let domain = "";
  try { domain = new URL(canonicalUrl).hostname.replace(/^www\./, ""); } catch { /* candidate URLs are normalized before this point */ }
  return { companyId: stableHash(`company:${normalizedText(company)}:${domain}`), companyDomain: domain };
}

function refreshCompany(companies, job) {
  const identity = companyIdentity(job.company, job.canonicalUrl);
  const existing = companies[identity.companyId] || { companyId: identity.companyId, companyName: job.company, companyDomain: identity.companyDomain, platforms: [], careerUrls: [], firstSeenAt: job.firstSeenAt, notes: "" };
  existing.companyName = job.company || existing.companyName;
  existing.companyDomain = identity.companyDomain || existing.companyDomain;
  existing.platforms = [...new Set([...existing.platforms, job.platform])];
  existing.careerUrls = [...new Set([...existing.careerUrls, new URL(job.canonicalUrl).origin])];
  existing.lastSeenAt = job.lastSeenAt;
  existing.remoteHiringSignal = job.remoteStatus === "verified" ? "verified" : existing.remoteHiringSignal || "unsure";
  existing.status = "active";
  companies[identity.companyId] = existing;
  return identity.companyId;
}

function companyNameFromCareerUrl(url, fallback = "") {
  try {
    const value = decodeURIComponent(new URL(url).pathname.split("/").filter(Boolean)[0] || "");
    return value.replace(/[-_]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase()) || fallback;
  } catch { return fallback; }
}

function recordCompanyBoard(state, candidate, now, reason = "company_careers_landing_page") {
  const previous = state.jobs[candidate.jobId];
  const job = {
    ...candidate,
    title: candidate.title || previous?.title || "Company careers board",
    description: candidate.snippet || previous?.description || "",
    company: previous?.company || companyNameFromCareerUrl(candidate.canonicalUrl, candidate.displayLink),
    location: "",
    juniorStatus: "not_applicable",
    remoteStatus: "not_applicable",
    pythonStatus: "not_applicable",
    evidenceText: "Company careers landing page; not an individual job posting.",
    score: 0,
    reviewReason: "Company careers landing page, not a job posting",
    closed: false,
    firstSeenAt: previous?.firstSeenAt || now,
    lastSeenAt: now,
    verificationCheckedAt: now,
    misses: 0,
    status: "company_board",
    recordType: reason
  };
  job.companyId = refreshCompany(state.companies, job);
  state.jobs[job.jobId] = job;
  return !previous;
}

function recalculateCompanies(state) {
  for (const company of Object.values(state.companies)) {
    company.activeJobCount = 0;
    company.remoteHiringSignal = "unsure";
  }
  for (const job of Object.values(state.jobs)) {
    const company = state.companies[job.companyId];
    if (["active", "review", "possibly_closed"].includes(job.status) && company) company.activeJobCount += 1;
    if (job.remoteStatus === "verified" && company) company.remoteHiringSignal = "verified";
  }
  for (const company of Object.values(state.companies)) company.status = company.activeJobCount ? "active" : "inactive";
}

function querySequence(allQueries, state, suppliedQueries) {
  if (suppliedQueries) return suppliedQueries;
  const idIndex = state.queryCursorId ? allQueries.findIndex((query) => query.id === state.queryCursorId) : -1;
  const cursor = idIndex >= 0 ? idIndex : Math.min(state.queryCursor || 0, Math.max(0, allQueries.length - 1));
  return [...allQueries.slice(cursor), ...allQueries.slice(0, cursor)];
}

function advanceQueryCursor(state, allQueries, query) {
  const index = allQueries.findIndex((item) => item.id === query.id);
  if (index < 0) return;
  const nextIndex = (index + 1) % allQueries.length;
  state.queryCursor = nextIndex;
  state.queryCursorId = allQueries[nextIndex]?.id || "";
  if (nextIndex === 0) state.queryCycle = (state.queryCycle || 1) + 1;
}

function recordQueryMisses(state, query, seenJobIds, closeAfterMisses) {
  for (const job of Object.values(state.jobs)) {
    if (["test", "company_board"].includes(job.status) || !job.sourceQueries?.includes(query.id)) continue;
    if (seenJobIds.has(job.jobId)) { job.misses = 0; continue; }
    job.misses = (job.misses || 0) + 1;
    job.status = job.misses >= closeAfterMisses ? "closed" : "possibly_closed";
  }
}

async function waitWithHeartbeat(ms, { stateStore, runId, shouldStop }) {
  let remaining = Math.max(0, ms);
  while (remaining > 0 && !shouldStop?.()) {
    const chunk = Math.min(remaining, 30_000);
    await delay(chunk);
    remaining -= chunk;
    await stateStore.heartbeatRunLock(runId);
  }
}

export async function syncStateToSheets(sheets, state, stateStore) {
  const finalJobs = Object.values(state.jobs).filter((job) => job.status !== "test");
  const finalCompanyIds = new Set(finalJobs.map((job) => job.companyId));
  const finalCompanies = Object.values(state.companies).filter((company) => finalCompanyIds.has(company.companyId));
  const allJobRows = finalJobs.map((job) => ({ id: job.jobId, values: jobRow(job) }));
  const allCompanyRows = finalCompanies.map((company) => ({ id: company.companyId, values: companyRow(company) }));
  const jobs = await stateStore.projectionChanges?.("Jobs", allJobRows) || allJobRows;
  const companies = await stateStore.projectionChanges?.("Companies", allCompanyRows) || allCompanyRows;
  const reviews = finalJobs.filter((job) => job.status === "review").map((job) => [job.jobId, job.company, job.title, job.canonicalUrl, job.reviewReason, job.juniorStatus, job.remoteStatus, job.pythonStatus, job.verificationCheckedAt]);
  const pendingRuns = Object.values(state.runs).filter((item) => !item.sheetSyncedAt);
  const runs = pendingRuns.map((item) => ({ id: item.id, values: runRow(item) }));
  const projection = await sheets.syncProjection?.({ jobIds: finalJobs.map((job) => job.jobId), companyIds: finalCompanies.map((company) => company.companyId), jobs, companies, reviews, runs });
  if (!projection?.supported) {
    if (sheets.retain) {
      await sheets.retain("Jobs", finalJobs.map((job) => job.jobId));
      await sheets.retain("Companies", finalCompanies.map((company) => company.companyId));
    }
    await sheets.upsert("Jobs", jobs);
    await sheets.upsert("Companies", companies);
    if (sheets.replace) await sheets.replace("Review Queue", reviews);
    else if (reviews.length) await sheets.upsert("Review Queue", reviews.map((values) => ({ id: values[0], values })));
    await sheets.upsert("Runs", runs);
  }
  await stateStore.markProjectionSynced?.("Jobs", jobs);
  await stateStore.markProjectionSynced?.("Companies", companies);
  const syncedAt = isoNow();
  for (const item of pendingRuns) item.sheetSyncedAt = syncedAt;
}

export async function reverifyStoredJobs({ stateStore, sheets, signalRules = {} }) {
  const runId = `reverify_${Date.now()}`;
  const staleAfterMs = 360 * 60_000;
  if (!await stateStore.acquireRunLock(runId, { staleAfterMs })) throw new Error("A run is already active.");
  try {
    const state = await stateStore.read();
    if (state.activeRunId) throw new Error("A discovery run is already active.");
    let checked = 0;
    let changed = 0;
    for (const job of Object.values(state.jobs)) {
      if (job.status === "test") continue;
      if (isCareerLandingPageUrl(job.canonicalUrl, job.platform)) {
        const previous = JSON.stringify({ status: job.status, reviewReason: job.reviewReason, remoteStatus: job.remoteStatus, juniorStatus: job.juniorStatus, pythonStatus: job.pythonStatus });
        Object.assign(job, {
          status: "company_board",
          recordType: "company_careers_landing_page",
          reviewReason: "Company careers landing page, not a job posting",
          juniorStatus: "not_applicable",
          remoteStatus: "not_applicable",
          pythonStatus: "not_applicable",
          evidenceText: "Company careers landing page; not an individual job posting.",
          score: 0,
          verificationCheckedAt: isoNow()
        });
        if (previous !== JSON.stringify({ status: job.status, reviewReason: job.reviewReason, remoteStatus: job.remoteStatus, juniorStatus: job.juniorStatus, pythonStatus: job.pythonStatus })) changed += 1;
        checked += 1;
        continue;
      }
      const signals = verifySignals({ title: job.title, description: job.description, location: job.location, role: job.role }, signalRules);
      const previous = JSON.stringify({ juniorStatus: job.juniorStatus, remoteStatus: job.remoteStatus, pythonStatus: job.pythonStatus, evidenceText: job.evidenceText, score: job.score, reviewReason: job.reviewReason, status: job.status });
      Object.assign(job, signals, { verificationCheckedAt: isoNow() });
      if (["active", "review"].includes(job.status)) job.status = signals.reviewReason ? "review" : "active";
      const current = JSON.stringify({ juniorStatus: job.juniorStatus, remoteStatus: job.remoteStatus, pythonStatus: job.pythonStatus, evidenceText: job.evidenceText, score: job.score, reviewReason: job.reviewReason, status: job.status });
      if (previous !== current) changed += 1;
      checked += 1;
    }
    recalculateCompanies(state);
    await stateStore.write(state);
    if (sheets) {
      await syncStateToSheets(sheets, state, stateStore);
      await stateStore.write(state);
    }
    return { checked, changed, sheetSynced: Boolean(sheets) };
  } finally {
    await stateStore.releaseRunLock();
  }
}

export async function runDiscovery({ trigger = "manual", stateStore, searchProvider, sheets, settings, queryInventory = buildQueries(), queries: suppliedQueries, listingReader = readListing, signalRules = {}, testMode = false, forceHydration = false, shouldStop = () => false, logger = console }) {
  const providedSettings = settings || {};
  settings = { ...defaults, ...providedSettings };
  if (!("minListingDelayMs" in providedSettings) && "minDelayMs" in providedSettings) settings.minListingDelayMs = providedSettings.minDelayMs;
  if (!("maxListingDelayMs" in providedSettings) && "maxDelayMs" in providedSettings) settings.maxListingDelayMs = providedSettings.maxDelayMs;
  const runId = `run_${Date.now()}`;
  const staleAfterMs = (settings.staleLockMinutes || 360) * 60_000;
  if (!await stateStore.acquireRunLock(runId, { staleAfterMs })) throw new Error("A run is already active.");
  const state = await stateStore.read();
  if (state.activeRunId) {
    const abandoned = state.runs[state.activeRunId];
    if (abandoned) {
      abandoned.status = "interrupted_recovered";
      abandoned.endedAt = abandoned.endedAt || isoNow();
      abandoned.errors = [...(abandoned.errors || []), `Recovered by ${runId} after an unclean shutdown.`];
    }
    state.activeRunId = null;
  }
  const hydratedBeforeRunToday = completedHydrationsToday(state, { timezone: settings.timezone, excludingRunId: runId });
  const run = { id: runId, trigger, status: "running", startedAt: isoNow(), endedAt: "", lastCheckpointAt: isoNow(), queriesAttempted: 0, queriesCompleted: 0, resultsFound: 0, uniqueCandidates: 0, hydrated: 0, hydratedByField: { engineering: 0, design: 0 }, dailyHydratedAtStart: hydratedBeforeRunToday.total, dailyHydratedByFieldAtStart: hydratedBeforeRunToday.byField, newJobs: 0, errors: [], stopReason: "" };
  const hydratedForFieldToday = (field) => hydratedBeforeRunToday.byField[discoveryField(field)] + run.hydratedByField[discoveryField(field)];
  const checkpoint = async () => {
    run.lastCheckpointAt = isoNow();
    state.activeRunId = run.id;
    state.runs[run.id] = run;
    await stateStore.write(state);
    await stateStore.heartbeatRunLock(run.id);
  };
  state.activeRunId = run.id; state.runs[run.id] = run; await checkpoint();
  let thrown;
  try {
    if (!queryInventory.length) throw new Error("No enabled queries are configured.");
    const orderedQueries = querySequence(queryInventory, state, suppliedQueries).slice(0, settings.maxQueriesPerRun);
    const seenInRun = new Map();
    for (const query of orderedQueries) {
      if (shouldStop() && run.queriesAttempted === 0) { run.stopReason = "shutdown_requested"; break; }
      const field = discoveryField(query.field);
      const existingProgress = suppliedQueries ? {} : (state.queryProgress?.[query.id] || {});
      if (hydratedForFieldToday(field) >= fieldQuota(settings, field)) continue;
      run.queriesAttempted += 1;
      let results;
      if (["hydrating", "pending_retry", "pending_quota"].includes(existingProgress.status) && existingProgress.searchResults?.length) {
        results = asArrayWithStop(existingProgress.searchResults, existingProgress.paginationStop || "resumed");
      } else {
        state.queryProgress[query.id] = { ...existingProgress, status: "searching", startedAt: existingProgress.startedAt || isoNow(), runId: run.id };
        await checkpoint();
        let searchError;
        for (let attempt = 1; attempt <= settings.searchRetryAttempts; attempt += 1) {
          try {
            const progress = state.queryProgress[query.id];
            results = await searchProvider.search(query, {
              resume: { partialResults: progress.partialResults || [], nextPage: progress.nextPage || 0, thinPages: progress.thinPages || 0 },
              maxPages: settings.maxPagesPerQuery, maxMinutes: settings.maxSearchMinutesPerQuery,
              minPageDelayMs: settings.minPageDelayMs, maxPageDelayMs: settings.maxPageDelayMs,
              searchPageBurstSize: settings.searchPageBurstSize,
              minSearchPageCooldownMs: settings.minSearchPageCooldownMs,
              maxSearchPageCooldownMs: settings.maxSearchPageCooldownMs,
              onPage: async (pageCheckpoint) => {
                await stateStore.recordSearchResults?.(query.id, pageCheckpoint.partialResults || []);
                state.queryProgress[query.id] = { ...state.queryProgress[query.id], ...pageCheckpoint, status: "searching", runId: run.id, checkpointedAt: isoNow() };
                await checkpoint();
              }
            });
            searchError = undefined;
            break;
          } catch (error) {
            searchError = error;
            state.queryProgress[query.id] = { ...state.queryProgress[query.id], status: "search_retry", runId: run.id, attempt, error: error.message, checkpointedAt: isoNow() };
            await checkpoint();
            // A challenge is an explicit instruction to stop requesting Google.
            // Keep the checkpoint and defer this query; do not immediately retry it.
            if (error?.name === "SearchBlockedError") break;
            if (attempt < settings.searchRetryAttempts) await waitWithHeartbeat(settings.retryBaseDelayMs * 2 ** (attempt - 1), { stateStore, runId, shouldStop: () => false });
          }
        }
        if (searchError) {
          run.errors.push(`${query.id}: ${searchError.message}`);
          state.queryProgress[query.id] = { ...state.queryProgress[query.id], status: "pending_retry", stage: "search", runId: run.id, error: searchError.message, interruptedAt: isoNow() };
          run.stopReason = "query_pending_retry";
          await checkpoint();
          break;
        }
      }

      await stateStore.recordSearchResults?.(query.id, results);
      run.resultsFound += results.length;
      state.queryProgress[query.id] = { ...state.queryProgress[query.id], status: "hydrating", stage: "listing", runId: run.id, searchResults: [...results], paginationStop: results.paginationStop || "exhausted", searchCompletedAt: isoNow() };
      await checkpoint();
      const queryCandidates = [];
      const companyBoardCandidates = [];
      const querySeenJobIds = new Set();
      for (const result of results.filter((item) => item.link?.startsWith("http"))) {
        const candidate = toCandidate(result, query);
        if (!isJobPostingCandidate(candidate)) {
          recordCompanyBoard(state, candidate, isoNow());
          companyBoardCandidates.push({ url: candidate.canonicalUrl, reason: "company_careers_landing_page" });
          continue;
        }
        querySeenJobIds.add(candidate.jobId);
        const prior = seenInRun.get(candidate.jobId);
        if (prior) {
          prior.sourceQueries = mergeSources(prior.sourceQueries, candidate.sourceQueries);
          if (state.jobs[prior.jobId]) state.jobs[prior.jobId].sourceQueries = mergeSources(state.jobs[prior.jobId].sourceQueries, candidate.sourceQueries);
          continue;
        }
        seenInRun.set(candidate.jobId, candidate);
        queryCandidates.push(candidate);
      }
      const recheckAfterMs = settings.recheckAfterDays * 24 * 60 * 60 * 1000;
      const indexedKnownJobs = await stateStore.lookupJobs?.(queryCandidates) || {};
      const { uniqueCandidates, hydrationQueue: dedupedHydrationQueue } = dedupeCandidates(queryCandidates, { ...state.jobs, ...indexedKnownJobs }, { recheckAfterMs });
      const hydrationQueue = forceHydration ? uniqueCandidates : dedupedHydrationQueue;
      run.uniqueCandidates += uniqueCandidates.length;
      const now = isoNow();
      for (const candidate of uniqueCandidates) {
        const previous = state.jobs[candidate.jobId];
        if (previous) {
          previous.lastSeenAt = now; previous.sourceQueries = mergeSources(previous.sourceQueries, candidate.sourceQueries); previous.misses = 0;
          if (previous.status === "possibly_closed" || previous.status === "closed") previous.status = previous.reviewReason ? "review" : "active";
        }
      }

      const failedCandidates = [];
      let deferredForQuota = false;
      for (const candidate of hydrationQueue) {
        if (hydratedForFieldToday(field) >= fieldQuota(settings, field)) {
          deferredForQuota = true;
          state.queryProgress[query.id] = {
            ...state.queryProgress[query.id], status: "pending_quota", stage: "listing", field,
            searchResults: [...results], paginationStop: results.paginationStop || "exhausted",
            deferredAt: isoNow(), deferredReason: `${field}_daily_listing_quota`
          };
          await checkpoint();
          break;
        }
        let listing;
        let listingError;
        for (let attempt = 1; attempt <= settings.listingRetryAttempts; attempt += 1) {
          try {
            listing = await listingReader(candidate, {
              minDwellMs: settings.minListingDwellMs,
              maxDwellMs: settings.maxListingDwellMs
            });
            listingError = undefined;
            break;
          }
          catch (error) {
            listingError = error;
            if (attempt < settings.listingRetryAttempts) await waitWithHeartbeat(settings.retryBaseDelayMs * 2 ** (attempt - 1), { stateStore, runId, shouldStop: () => false });
          }
        }
        if (listingError) {
          failedCandidates.push({ jobId: candidate.jobId, url: candidate.canonicalUrl, error: listingError.message });
          run.errors.push(`${candidate.canonicalUrl}: ${listingError.message}`);
        } else {
          if (listing.isJobPosting === false) {
            recordCompanyBoard(state, candidate, isoNow(), listing.exclusionReason || "careers_landing_page");
            companyBoardCandidates.push({ url: candidate.canonicalUrl, reason: listing.exclusionReason || "careers_landing_page" });
          } else {
            const signals = verifySignals({ title: listing.title, description: listing.description, location: listing.location, role: candidate.role }, signalRules);
            const previous = state.jobs[candidate.jobId];
            const job = {
              ...candidate, ...signals, ...listing,
              company: listing.company || previous?.company || candidate.displayLink,
              jobId: candidate.jobId, role: candidate.role, platform: candidate.platform,
              sourceQueries: mergeSources(previous?.sourceQueries, candidate.sourceQueries),
              firstSeenAt: previous?.firstSeenAt || now, lastSeenAt: now, verificationCheckedAt: isoNow(),
              misses: 0, status: testMode ? "test" : listing.closed ? "closed" : signals.reviewReason ? "review" : "active"
            };
            job.companyId = refreshCompany(state.companies, job);
            state.jobs[job.jobId] = job;
            if (!previous) run.newJobs += 1;
            run.hydrated += 1;
            run.hydratedByField[field] += 1;
          }
        }
        state.queryProgress[query.id] = { ...state.queryProgress[query.id], failedCandidates, companyBoardCandidates, lastCandidateId: candidate.jobId, hydratedSoFar: run.hydrated, checkpointedAt: isoNow() };
        await checkpoint();
        await waitWithHeartbeat(randomBetween(settings.minListingDelayMs ?? settings.minDelayMs ?? 0, settings.maxListingDelayMs ?? settings.maxDelayMs ?? 0), { stateStore, runId, shouldStop: () => false });
      }

      if (deferredForQuota) continue;

      if (failedCandidates.length) {
        state.queryProgress[query.id] = { ...state.queryProgress[query.id], status: "pending_retry", stage: "listing", failedCandidates, runId: run.id, interruptedAt: isoNow() };
        run.stopReason = "query_pending_retry";
        await checkpoint();
        break;
      }

      recordQueryMisses(state, query, querySeenJobIds, settings.closeAfterMisses);
      state.queryProgress[query.id] = { status: "completed", completionReason: results.paginationStop || "exhausted", completedAt: isoNow(), runId: run.id, resultsFound: results.length, uniqueCandidates: uniqueCandidates.length, companyBoardCandidates };
      run.queriesCompleted += 1;
      if (!suppliedQueries) advanceQueryCursor(state, queryInventory, query);
      await checkpoint();
      if (["engineering", "design"].every((candidateField) => hydratedForFieldToday(candidateField) >= fieldQuota(settings, candidateField))) {
        run.stopReason = "daily_listing_target";
        break;
      }
      if (shouldStop()) { run.stopReason = "shutdown_requested"; break; }
      if (query === orderedQueries.at(-1)) break;
      const delayRange = run.queriesCompleted % settings.queryBurstSize === 0
        ? [settings.cooldownMinMs, settings.cooldownMaxMs]
        : [settings.minQueryDelayMs ?? settings.minDelayMs ?? 0, settings.maxQueryDelayMs ?? settings.maxDelayMs ?? 0];
      await waitWithHeartbeat(randomBetween(...delayRange), { stateStore, runId, shouldStop });
    }
    if (!run.stopReason && hydratedBeforeRunToday.total + run.hydrated >= settings.maxListingsPerRun) run.stopReason = "daily_listing_target";
    recalculateCompanies(state);
    run.status = run.errors.length ? "completed_with_errors" : "completed";
  } catch (error) {
    thrown = error;
    run.status = "failed";
    run.errors.push(error.message);
  } finally {
    run.endedAt = isoNow();
    state.activeRunId = null;
    state.runs[run.id] = run;
    await stateStore.write(state);
    try { await searchProvider.close?.(); } catch (error) { logger.error(`Browser cleanup failed: ${error.message}`); }
    if (sheets) {
      try {
        await syncStateToSheets(sheets, state, stateStore);
        await stateStore.write(state);
      } catch (sheetError) {
        run.errors.push(`Google Sheets sync failed: ${sheetError.message}`);
        if (run.status !== "failed") run.status = "completed_with_sync_error";
        state.runs[run.id] = run;
        await stateStore.write(state);
        logger.error(`Google Sheets sync failed: ${sheetError.message}`);
      }
    }
    await stateStore.releaseRunLock();
  }
  if (thrown) throw thrown;
  return run;
}
