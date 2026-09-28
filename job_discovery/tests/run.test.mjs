import test from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import fs from "node:fs/promises";
import { StateStore } from "../src/state-store.mjs";
import { runDiscovery } from "../src/run.mjs";

test("deduplicates before reading a listing and avoids re-reading an unchanged verified job", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "job-discovery-"));
  const stateStore = new StateStore(path.join(directory, "state.json"));
  const searchProvider = { search: async () => [
    { title: "Junior Python Developer", link: "https://jobs.ashbyhq.com/acme/abcdefgh?utm_source=one", snippet: "Remote Python role" },
    { title: "Junior Python Developer", link: "https://jobs.ashbyhq.com/acme/abcdefgh?utm_source=two", snippet: "Remote Python role" }
  ] };
  let reads = 0;
  const listingReader = async () => { reads += 1; return { title: "Junior Python Developer", description: "Remote role using Python.", company: "Acme", location: "Remote", canonicalUrl: "https://jobs.ashbyhq.com/acme/abcdefgh" }; };
  const settings = { maxQueriesPerRun: 1, maxListingsPerRun: 5, minDelayMs: 0, maxDelayMs: 0, queryBurstSize: 99, cooldownMinMs: 0, cooldownMaxMs: 0, maxConsecutiveErrorsPerPlatform: 3, closeAfterMisses: 3 };
  const first = await runDiscovery({ stateStore, searchProvider, listingReader, settings });
  const second = await runDiscovery({ stateStore, searchProvider, listingReader, settings });
  assert.equal(first.uniqueCandidates, 1);
  assert.equal(first.hydrated, 1);
  assert.equal(second.hydrated, 0);
  assert.equal(reads, 1);
});

test("finishes the current query before stopping at the daily listing target and advances the cursor", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "job-discovery-rotation-"));
  const stateStore = new StateStore(path.join(directory, "state.json"));
  const searchProvider = { search: async () => [{ title: "Junior Python Developer", link: "https://jobs.ashbyhq.com/acme/rotating-job", snippet: "Remote Python role" }] };
  const listingReader = async () => ({ title: "Junior Python Developer", description: "Remote role using Python.", company: "Acme", location: "Remote", canonicalUrl: "https://jobs.ashbyhq.com/acme/rotating-job" });
  const settings = { maxQueriesPerRun: 10, maxListingsPerRun: 1, minDelayMs: 0, maxDelayMs: 0, queryBurstSize: 99, cooldownMinMs: 0, cooldownMaxMs: 0, maxConsecutiveErrorsPerPlatform: 3, closeAfterMisses: 3 };
  const run = await runDiscovery({ stateStore, searchProvider, listingReader, settings });
  const state = await stateStore.read();
  assert.equal(run.queriesAttempted, 1);
  assert.equal(state.queryCursor, 1);
  assert.equal(state.queryProgress["Ashby:Python Developer:junior"].status, "completed");
});

test("counts earlier same-day runs toward the daily listing target", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "job-discovery-daily-total-"));
  const stateStore = new StateStore(path.join(directory, "state.json"));
  const prior = await stateStore.read();
  prior.runs.prior = { id: "prior", trigger: "manual", startedAt: new Date().toISOString(), hydrated: 1, hydratedByField: { design: 1 } };
  await stateStore.write(prior);
  const queries = [
    { id: "q1", platform: "Ashby", role: "Python Developer", type: "junior", query: "one" },
    { id: "q2", platform: "Ashby", role: "Python Developer", type: "junior", query: "two" }
  ];
  const searchProvider = { search: async (query) => [{ title: `Junior Python Developer ${query.id}`, link: `https://jobs.ashbyhq.com/acme/${query.id}-role`, snippet: "Remote" }] };
  const listingReader = async (candidate) => ({ title: candidate.title, description: "Remote Python", company: "Acme", location: "Remote", canonicalUrl: candidate.canonicalUrl });
  const run = await runDiscovery({ stateStore, searchProvider, listingReader, settings: { timezone: "UTC", maxQueriesPerRun: 2, maxListingsPerRun: 2, minDelayMs: 0, maxDelayMs: 0, minQueryDelayMs: 0, maxQueryDelayMs: 0, queryBurstSize: 99, cooldownMinMs: 0, cooldownMaxMs: 0 }, queryInventory: queries });
  assert.equal(run.dailyHydratedAtStart, 1);
  assert.equal(run.queriesAttempted, 1);
  assert.equal(run.stopReason, "daily_listing_target");
});

test("pauses a field's remaining hydration queue at its daily allocation and continues the other field", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "job-discovery-field-quota-"));
  const stateStore = new StateStore(path.join(directory, "state.json"));
  const engineering = { id: "engineering", platform: "Ashby", role: "Backend Developer", field: "engineering", type: "junior", query: "engineering" };
  const design = { id: "design", platform: "Ashby", role: "Product Designer", field: "design", type: "junior", query: "design" };
  const searchProvider = { search: async (query) => [1, 2].map((number) => ({ title: `${query.role} ${number}`, link: `https://jobs.ashbyhq.com/acme/${query.id}-${number}`, snippet: "Remote" })) };
  const listingReader = async (candidate) => ({ title: candidate.title, description: "Remote", company: "Acme", location: "Remote", canonicalUrl: candidate.canonicalUrl });
  const run = await runDiscovery({ stateStore, searchProvider, listingReader, settings: { maxQueriesPerRun: 2, maxListingsPerRun: 2, minDelayMs: 0, maxDelayMs: 0, minQueryDelayMs: 0, maxQueryDelayMs: 0, queryBurstSize: 99, cooldownMinMs: 0, cooldownMaxMs: 0 }, queryInventory: [engineering, design] });
  const state = await stateStore.read();
  assert.deepEqual(run.hydratedByField, { engineering: 1, design: 1 });
  assert.equal(state.queryProgress.engineering.status, "pending_quota");
  assert.equal(state.queryProgress.design.status, "pending_quota");
  assert.equal(run.hydrated, 2);
});

test("records Ashby company careers pages without listing hydration", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "job-discovery-careers-page-"));
  const stateStore = new StateStore(path.join(directory, "state.json"));
  const searchProvider = { search: async () => [{ title: "Open Positions (7)", link: "https://jobs.ashbyhq.com/cradlebio", snippet: "Careers" }] };
  let reads = 0;
  const listingReader = async () => { reads += 1; return {}; };
  const run = await runDiscovery({ stateStore, searchProvider, listingReader, settings: { maxQueriesPerRun: 1, maxListingsPerRun: 5, minDelayMs: 0, maxDelayMs: 0, queryBurstSize: 99, cooldownMinMs: 0, cooldownMaxMs: 0 } });
  const state = await stateStore.read();
  assert.equal(reads, 0);
  assert.equal(run.uniqueCandidates, 0);
  assert.equal(Object.values(state.jobs)[0].status, "company_board");
  assert.deepEqual(state.queryProgress["Ashby:Python Developer:junior"].companyBoardCandidates, [{ url: "https://jobs.ashbyhq.com/cradlebio", reason: "company_careers_landing_page" }]);
});

test("keeps the cursor on a failed listing and resumes hydration without repeating Google search", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "job-discovery-retry-"));
  const stateStore = new StateStore(path.join(directory, "state.json"));
  const query = { id: "q1", platform: "Ashby", role: "Python Developer", type: "junior", query: "fixture", allowedHosts: ["jobs.ashbyhq.com"] };
  let searches = 0;
  const searchProvider = { search: async () => { searches += 1; return [{ title: "Junior Python Developer", link: "https://jobs.ashbyhq.com/acme/retry-job", snippet: "Remote Python" }]; } };
  let fail = true;
  const listingReader = async () => { if (fail) throw new Error("temporary ATS failure"); return { title: "Junior Python Developer", description: "Remote Python", company: "Acme", location: "Remote", canonicalUrl: "https://jobs.ashbyhq.com/acme/retry-job" }; };
  const settings = { maxQueriesPerRun: 1, maxListingsPerRun: 5, minDelayMs: 0, maxDelayMs: 0, queryBurstSize: 10, cooldownMinMs: 0, cooldownMaxMs: 0, searchRetryAttempts: 1, listingRetryAttempts: 1, retryBaseDelayMs: 0 };
  const first = await runDiscovery({ stateStore, searchProvider, listingReader, settings, queryInventory: [query] });
  let state = await stateStore.read();
  assert.equal(first.stopReason, "query_pending_retry");
  assert.equal(state.queryCursor, 0);
  assert.equal(state.queryProgress.q1.status, "pending_retry");
  fail = false;
  const second = await runDiscovery({ stateStore, searchProvider, listingReader, settings, queryInventory: [query] });
  state = await stateStore.read();
  assert.equal(second.hydrated, 1);
  assert.equal(searches, 1);
  assert.equal(state.queryProgress.q1.status, "completed");
});

test("does not immediately retry a Google verification challenge", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "job-discovery-blocked-search-"));
  const stateStore = new StateStore(path.join(directory, "state.json"));
  const query = { id: "q1", platform: "Ashby", role: "Python Developer", type: "junior", query: "fixture", allowedHosts: ["jobs.ashbyhq.com"] };
  let searches = 0;
  const searchProvider = {
    search: async () => {
      searches += 1;
      const error = new Error("Google presented a verification page");
      error.name = "SearchBlockedError";
      throw error;
    }
  };
  const run = await runDiscovery({ stateStore, searchProvider, settings: { maxQueriesPerRun: 1, searchRetryAttempts: 3, retryBaseDelayMs: 0 }, queryInventory: [query] });
  const state = await stateStore.read();
  assert.equal(searches, 1);
  assert.equal(run.stopReason, "query_pending_retry");
  assert.equal(state.queryProgress.q1.status, "pending_retry");
});

test("moves a job to closed only after repeated misses from its completed source query", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "job-discovery-lifecycle-"));
  const stateStore = new StateStore(path.join(directory, "state.json"));
  const query = { id: "q1", platform: "Ashby", role: "Python Developer", type: "junior", query: "fixture" };
  const seedSearch = { search: async () => [{ title: "Junior Python Developer", link: "https://jobs.ashbyhq.com/acme/lifecycle-job", snippet: "Remote Python" }] };
  const listingReader = async () => ({ title: "Junior Python Developer", description: "Remote Python", company: "Acme", location: "Remote", canonicalUrl: "https://jobs.ashbyhq.com/acme/lifecycle-job" });
  const settings = { maxQueriesPerRun: 1, maxListingsPerRun: 5, minDelayMs: 0, maxDelayMs: 0, searchRetryAttempts: 1, listingRetryAttempts: 1, retryBaseDelayMs: 0, closeAfterMisses: 3 };
  await runDiscovery({ stateStore, searchProvider: seedSearch, listingReader, settings, queries: [query] });
  const emptySearch = { search: async () => [] };
  await runDiscovery({ stateStore, searchProvider: emptySearch, listingReader, settings, queries: [query] });
  let state = await stateStore.read();
  assert.equal(Object.values(state.jobs)[0].status, "possibly_closed");
  await runDiscovery({ stateStore, searchProvider: emptySearch, listingReader, settings, queries: [query] });
  await runDiscovery({ stateStore, searchProvider: emptySearch, listingReader, settings, queries: [query] });
  state = await stateStore.read();
  assert.equal(Object.values(state.jobs)[0].status, "closed");
});
