import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import crypto from "node:crypto";
import { DatabaseSync } from "node:sqlite";

const blankState = () => ({ jobs: {}, companies: {}, runs: {}, liveTestUsage: {}, queryProgress: {}, queryCursor: 0, queryCursorId: "", queryCycle: 1, lastScheduledDate: "", activeRunId: null, schemaVersion: 3 });
const json = (value) => JSON.stringify(value);
const hash = (value) => crypto.createHash("sha256").update(value).digest("hex").slice(0, 24);

function processIsAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; }
  catch (error) { return error.code === "EPERM"; }
}

export class StateStore {
  constructor(filePath) {
    const resolved = path.resolve(filePath);
    this.legacyJsonPath = resolved.endsWith(".json") ? resolved : undefined;
    this.filePath = resolved.endsWith(".json") ? resolved.replace(/\.json$/i, ".sqlite") : resolved;
    this.lockPath = `${this.filePath}.lock`;
    this.db = undefined;
    this.cache = new Map();
  }

  async ensureDatabase() {
    if (this.db) return;
    await fs.mkdir(path.dirname(this.filePath), { recursive: true });
    let legacy;
    if (this.legacyJsonPath) {
      try { legacy = JSON.parse(await fs.readFile(this.legacyJsonPath, "utf8")); }
      catch (error) { if (error.code !== "ENOENT") throw error; }
    }
    this.db = new DatabaseSync(this.filePath);
    this.db.exec(`
      PRAGMA journal_mode = WAL;
      PRAGMA synchronous = NORMAL;
      PRAGMA busy_timeout = 5000;
      CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, canonical_url TEXT, fallback_fingerprint TEXT, result_hash TEXT,
        status TEXT, last_seen_at TEXT, data TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS jobs_fallback_idx ON jobs(fallback_fingerprint);
      CREATE INDEX IF NOT EXISTS jobs_url_idx ON jobs(canonical_url);
      CREATE TABLE IF NOT EXISTS companies (id TEXT PRIMARY KEY, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started_at TEXT, status TEXT, sheet_synced_at TEXT, data TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS runs_sync_idx ON runs(sheet_synced_at);
      CREATE TABLE IF NOT EXISTS query_progress (id TEXT PRIMARY KEY, status TEXT, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS live_test_usage (query_id TEXT PRIMARY KEY, uses INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS search_results (
        query_id TEXT NOT NULL, url_hash TEXT NOT NULL, url TEXT NOT NULL, title TEXT, snippet TEXT,
        result_hash TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        PRIMARY KEY(query_id, url_hash)
      );
      CREATE INDEX IF NOT EXISTS search_results_url_idx ON search_results(url_hash);
      CREATE INDEX IF NOT EXISTS search_results_result_hash_idx ON search_results(result_hash);
      CREATE TABLE IF NOT EXISTS projection_sync (
        target TEXT NOT NULL, id TEXT NOT NULL, projection_hash TEXT NOT NULL, synced_at TEXT NOT NULL,
        PRIMARY KEY(target, id)
      );
    `);
    for (const table of ["jobs", "companies", "runs", "query_progress"]) {
      for (const row of this.db.prepare(`SELECT id, data FROM ${table}`).all()) this.cache.set(`${table}:${row.id}`, row.data);
    }
    const hasState = this.db.prepare("SELECT 1 AS present FROM metadata LIMIT 1").get();
    if (!hasState && legacy) await this.write({ ...blankState(), ...legacy });
  }

  async acquireRunLock(runId, { staleAfterMs = 6 * 60 * 60 * 1000 } = {}) {
    await this.ensureDatabase();
    const lock = { runId, pid: process.pid, hostname: os.hostname(), createdAt: new Date().toISOString(), heartbeatAt: new Date().toISOString() };
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const handle = await fs.open(this.lockPath, "wx");
        await handle.writeFile(`${JSON.stringify(lock)}\n`); await handle.close();
        return true;
      } catch (error) {
        if (error.code !== "EEXIST") throw error;
        let stale = false;
        try {
          const [source, stats] = await Promise.all([fs.readFile(this.lockPath, "utf8"), fs.stat(this.lockPath)]);
          let existing;
          try { existing = JSON.parse(source); } catch { existing = {}; }
          const sameHostDeadProcess = existing.hostname === os.hostname() && existing.pid && !processIsAlive(Number(existing.pid));
          stale = sameHostDeadProcess || Date.now() - stats.mtimeMs > staleAfterMs;
        } catch (readError) {
          if (readError.code !== "ENOENT") throw readError;
          stale = true;
        }
        if (!stale) return false;
        try { await fs.unlink(this.lockPath); } catch (unlinkError) { if (unlinkError.code !== "ENOENT") throw unlinkError; }
      }
    }
    return false;
  }

  async heartbeatRunLock(runId) {
    const source = await fs.readFile(this.lockPath, "utf8");
    let lock;
    try { lock = JSON.parse(source); } catch { throw new Error("Run lock is malformed"); }
    if (lock.runId !== runId) throw new Error(`Run lock belongs to ${lock.runId || "another process"}`);
    lock.heartbeatAt = new Date().toISOString();
    await fs.writeFile(this.lockPath, `${JSON.stringify(lock)}\n`);
  }

  async releaseRunLock() {
    try { await fs.unlink(this.lockPath); } catch (error) { if (error.code !== "ENOENT") throw error; }
  }

  rowsAsMap(table) {
    return Object.fromEntries(this.db.prepare(`SELECT id, data FROM ${table}`).all().map((row) => [row.id, JSON.parse(row.data)]));
  }

  async read() {
    await this.ensureDatabase();
    const state = blankState();
    for (const row of this.db.prepare("SELECT key, value FROM metadata").all()) state[row.key] = JSON.parse(row.value);
    state.jobs = this.rowsAsMap("jobs");
    state.companies = this.rowsAsMap("companies");
    state.runs = this.rowsAsMap("runs");
    state.queryProgress = this.rowsAsMap("query_progress");
    state.liveTestUsage = Object.fromEntries(this.db.prepare("SELECT query_id, uses FROM live_test_usage").all().map((row) => [row.query_id, row.uses]));
    return state;
  }

  syncRecords(table, records, columns) {
    const ids = new Set(Object.keys(records));
    const existing = this.db.prepare(`SELECT id FROM ${table}`).all().map((row) => row.id);
    const columnNames = ["id", ...columns.map((item) => item[0]), "data"];
    const placeholders = columnNames.map(() => "?").join(",");
    const updates = [...columns.map(([name]) => `${name}=excluded.${name}`), "data=excluded.data"].join(",");
    const statement = this.db.prepare(`INSERT INTO ${table}(${columnNames.join(",")}) VALUES(${placeholders}) ON CONFLICT(id) DO UPDATE SET ${updates}`);
    for (const [id, value] of Object.entries(records)) {
      const data = json(value);
      const cacheKey = `${table}:${id}`;
      if (this.cache.get(cacheKey) === data) continue;
      statement.run(id, ...columns.map(([, getter]) => getter(value)), data);
      this.cache.set(cacheKey, data);
    }
    const remove = this.db.prepare(`DELETE FROM ${table} WHERE id = ?`);
    for (const id of existing) if (!ids.has(id)) { remove.run(id); this.cache.delete(`${table}:${id}`); }
  }

  transaction(action) {
    this.db.exec("BEGIN IMMEDIATE");
    try { const result = action(); this.db.exec("COMMIT"); return result; }
    catch (error) {
      this.db.exec("ROLLBACK");
      this.cache.clear();
      for (const table of ["jobs", "companies", "runs", "query_progress"]) {
        for (const row of this.db.prepare(`SELECT id, data FROM ${table}`).all()) this.cache.set(`${table}:${row.id}`, row.data);
      }
      throw error;
    }
  }

  async write(state) {
    await this.ensureDatabase();
    this.transaction(() => {
      const setMetadata = this.db.prepare("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value");
      for (const key of ["queryCursor", "queryCursorId", "queryCycle", "lastScheduledDate", "activeRunId", "schemaVersion"]) setMetadata.run(key, json(state[key] ?? blankState()[key]));
      this.syncRecords("jobs", state.jobs || {}, [
        ["canonical_url", (item) => item.canonicalUrl || ""], ["fallback_fingerprint", (item) => item.fallbackFingerprint || ""],
        ["result_hash", (item) => item.resultHash || ""], ["status", (item) => item.status || ""], ["last_seen_at", (item) => item.lastSeenAt || ""]
      ]);
      this.syncRecords("companies", state.companies || {}, []);
      this.syncRecords("runs", state.runs || {}, [
        ["started_at", (item) => item.startedAt || ""], ["status", (item) => item.status || ""], ["sheet_synced_at", (item) => item.sheetSyncedAt || null]
      ]);
      this.syncRecords("query_progress", state.queryProgress || {}, [["status", (item) => item.status || ""]]);
      const usage = this.db.prepare("INSERT INTO live_test_usage(query_id,uses) VALUES(?,?) ON CONFLICT(query_id) DO UPDATE SET uses=excluded.uses");
      for (const [queryId, uses] of Object.entries(state.liveTestUsage || {})) usage.run(queryId, Number(uses) || 0);
    });
  }

  async recordSearchResults(queryId, results) {
    if (!results?.length) return;
    await this.ensureDatabase();
    const now = new Date().toISOString();
    const statement = this.db.prepare(`
      INSERT INTO search_results(query_id,url_hash,url,title,snippet,result_hash,first_seen_at,last_seen_at)
      VALUES(?,?,?,?,?,?,?,?)
      ON CONFLICT(query_id,url_hash) DO UPDATE SET title=excluded.title,snippet=excluded.snippet,result_hash=excluded.result_hash,last_seen_at=excluded.last_seen_at
    `);
    this.transaction(() => {
      for (const item of results) {
        const resultHash = hash(`${item.title || ""}|${item.snippet || ""}|${item.link || ""}`);
        statement.run(queryId, hash(item.link || ""), item.link || "", item.title || "", item.snippet || "", resultHash, now, now);
      }
    });
  }

  async deleteSearchResultsForQuery(queryId) {
    await this.ensureDatabase();
    this.db.prepare("DELETE FROM search_results WHERE query_id = ?").run(queryId);
  }

  async lookupJobs(candidates) {
    await this.ensureDatabase();
    const byId = this.db.prepare("SELECT id, data FROM jobs WHERE id = ?");
    const byFallback = this.db.prepare("SELECT id, data FROM jobs WHERE fallback_fingerprint = ? LIMIT 1");
    const found = {};
    for (const candidate of candidates) {
      const row = byId.get(candidate.jobId) || (candidate.fallbackFingerprint ? byFallback.get(candidate.fallbackFingerprint) : undefined);
      if (row) found[row.id] = JSON.parse(row.data);
    }
    return found;
  }

  async stats() {
    await this.ensureDatabase();
    const count = (table) => Number(this.db.prepare(`SELECT COUNT(*) AS count FROM ${table}`).get().count);
    return { databasePath: this.filePath, jobs: count("jobs"), companies: count("companies"), runs: count("runs"), queryProgress: count("query_progress"), searchResults: count("search_results") };
  }

  async reset() {
    await this.ensureDatabase();
    this.transaction(() => {
      for (const table of ["metadata", "jobs", "companies", "runs", "query_progress", "live_test_usage", "search_results", "projection_sync"]) {
        this.db.exec(`DELETE FROM ${table}`);
      }
    });
    this.cache.clear();
    if (this.legacyJsonPath) await fs.writeFile(this.legacyJsonPath, `${json(blankState())}\n`);
    await this.write(blankState());
    return this.stats();
  }

  async projectionChanges(target, records, { reconcileAfterMs = 7 * 24 * 60 * 60 * 1000 } = {}) {
    await this.ensureDatabase();
    const existing = new Map(this.db.prepare("SELECT id, projection_hash, synced_at FROM projection_sync WHERE target = ?").all(target).map((row) => [row.id, row]));
    return records.filter((record) => {
      const previous = existing.get(record.id);
      const projectionHash = hash(json(record.values));
      return !previous || previous.projection_hash !== projectionHash || Date.now() - Date.parse(previous.synced_at) >= reconcileAfterMs;
    });
  }

  async markProjectionSynced(target, records) {
    if (!records.length) return;
    await this.ensureDatabase();
    const statement = this.db.prepare("INSERT INTO projection_sync(target,id,projection_hash,synced_at) VALUES(?,?,?,?) ON CONFLICT(target,id) DO UPDATE SET projection_hash=excluded.projection_hash,synced_at=excluded.synced_at");
    const syncedAt = new Date().toISOString();
    this.transaction(() => { for (const record of records) statement.run(target, record.id, hash(json(record.values)), syncedAt); });
  }
}
