import test from "node:test";
import assert from "node:assert/strict";
import { scheduledMoment, runIfDue } from "../src/scheduler.mjs";

test("uses the configured timezone when deciding whether the run is due", () => {
  const moment = scheduledMoment({ timezone: "Africa/Lagos" }, new Date("2026-08-20T07:00:00.000Z"));
  assert.deepEqual(moment, { dateKey: "2026-08-20", time: "08:00" });
});

test("runs later the same day when the exact scheduled minute was missed", async () => {
  let state = { jobs: {}, companies: {}, runs: {}, queryProgress: {}, liveTestUsage: {}, queryCursor: 0, queryCycle: 1, lastScheduledDate: "", activeRunId: null };
  const stateStore = { read: async () => state, write: async (next) => { state = next; }, acquireRunLock: async () => true, heartbeatRunLock: async () => {}, releaseRunLock: async () => {} };
  const query = { id: "q1", platform: "Ashby", role: "Python Developer", type: "junior", query: "fixture" };
  const result = await runIfDue({
    settings: { automationEnabled: true, timezone: "Africa/Lagos", dailyRunTime: "08:00", maxQueriesPerRun: 1, maxListingsPerRun: 1, minListingDelayMs: 0, maxListingDelayMs: 0, minQueryDelayMs: 0, maxQueryDelayMs: 0, queryBurstSize: 10, cooldownMinMs: 0, cooldownMaxMs: 0, searchRetryAttempts: 1, listingRetryAttempts: 1, retryBaseDelayMs: 0, maxPagesPerQuery: 2, maxSearchMinutesPerQuery: 1, recheckAfterDays: 7, staleLockMinutes: 360, closeAfterMisses: 3 },
    stateStore,
    dependencies: { queryInventory: [query], searchProvider: { search: async () => [] }, listingReader: async () => ({}) },
    date: new Date("2026-08-20T08:30:00.000Z")
  });
  assert.equal(result.started, true);
  assert.equal(state.lastScheduledDate, "2026-08-20");
});
