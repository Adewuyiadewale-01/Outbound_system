import test from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import fs from "node:fs/promises";
import { StateStore } from "../src/state-store.mjs";

test("migrates JSON state into indexed SQLite tables and records raw search history locally", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "job-discovery-sqlite-"));
  const legacyPath = path.join(directory, "state.json");
  await fs.writeFile(legacyPath, JSON.stringify({ jobs: { abc: { jobId: "abc", canonicalUrl: "https://jobs.ashbyhq.com/acme/abcde", fallbackFingerprint: "fallback" } }, queryCursor: 7 }));
  const store = new StateStore(legacyPath);
  const state = await store.read();
  assert.equal(state.queryCursor, 7);
  assert.equal(state.jobs.abc.jobId, "abc");
  await store.recordSearchResults("q1", [{ title: "Job", link: "https://jobs.ashbyhq.com/acme/abcde", snippet: "Remote" }]);
  assert.equal(store.db.prepare("SELECT COUNT(*) AS count FROM search_results").get().count, 1);
  assert.match(store.filePath, /\.sqlite$/);
});

test("recovers a lock owned by a dead local process", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "job-discovery-lock-"));
  const store = new StateStore(path.join(directory, "state.json"));
  await store.ensureDatabase();
  await fs.writeFile(store.lockPath, JSON.stringify({ runId: "dead", pid: 99999999, hostname: os.hostname() }));
  assert.equal(await store.acquireRunLock("replacement"), true);
  await store.releaseRunLock();
});

test("tracks only changed final Sheet projections for incremental synchronization", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "job-discovery-projection-"));
  const store = new StateStore(path.join(directory, "state.json"));
  const rows = [{ id: "job-1", values: ["job-1", "First"] }, { id: "job-2", values: ["job-2", "Second"] }];
  assert.equal((await store.projectionChanges("Jobs", rows)).length, 2);
  await store.markProjectionSynced("Jobs", rows);
  assert.equal((await store.projectionChanges("Jobs", rows)).length, 0);
  const changed = [{ id: "job-1", values: ["job-1", "Updated"] }, rows[1]];
  assert.deepEqual((await store.projectionChanges("Jobs", changed)).map((row) => row.id), ["job-1"]);
});

test("resets durable records, raw search history, and legacy state", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "job-discovery-reset-"));
  const legacyPath = path.join(directory, "state.json");
  const store = new StateStore(legacyPath);
  await store.write({ jobs: { job: { jobId: "job", canonicalUrl: "https://example.com/job" } }, companies: { company: { companyId: "company" } }, runs: { run: { id: "run" } }, queryProgress: { query: { id: "query" } }, liveTestUsage: { query: 2 }, queryCursor: 9, queryCursorId: "query" });
  await store.recordSearchResults("query", [{ title: "Job", link: "https://example.com/job" }]);
  await store.markProjectionSynced("Jobs", [{ id: "job", values: ["job"] }]);

  const stats = await store.reset();
  const state = await store.read();
  assert.deepEqual(stats.jobs, 0);
  assert.deepEqual(stats.companies, 0);
  assert.deepEqual(stats.runs, 0);
  assert.deepEqual(stats.queryProgress, 0);
  assert.deepEqual(stats.searchResults, 0);
  assert.equal(state.queryCursor, 0);
  assert.deepEqual(state.liveTestUsage, {});
  assert.equal((await store.projectionChanges("Jobs", [{ id: "job", values: ["job"] }])).length, 1);
  assert.deepEqual(JSON.parse(await fs.readFile(legacyPath, "utf8")).jobs, {});
});
