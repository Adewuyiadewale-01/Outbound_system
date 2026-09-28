import test from "node:test";
import assert from "node:assert/strict";
import { buildQueries } from "../src/query-builder.mjs";

test("builds one junior, unfiltered, and junior-signal query for every platform and role", () => {
  const queries = buildQueries();
  assert.equal(queries.length, 288);
  assert.equal(queries.filter((query) => query.type === "junior").length, 96);
  assert.equal(queries.filter((query) => query.field === "engineering").length, 180);
  assert.equal(queries.filter((query) => query.field === "design").length, 108);
  assert.deepEqual(queries.slice(0, 2).map((query) => query.field), ["engineering", "design"]);
  assert.match(queries.find((query) => query.id === "Lever:Python Developer:junior").query, /site:jobs\.lever\.co/);
  assert.match(queries.find((query) => query.id === "Ashby:Product Designer:junior").query, /product designer/);
  assert.match(queries.find((query) => query.id === "Ashby:Backend Developer:junior-signal").query, /"early career"/);
});
