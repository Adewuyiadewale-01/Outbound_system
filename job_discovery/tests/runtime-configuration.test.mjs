import test from "node:test";
import assert from "node:assert/strict";
import { configurationFromRows } from "../src/runtime-configuration.mjs";

test("uses enabled Sheet queries and platform domains as the runtime inventory", () => {
  const configuration = configurationFromRows({
    platformRows: [["Ashby", "site:jobs.ashbyhq.com", "TRUE"], ["Lever", "site:jobs.lever.co", "FALSE"]],
    queryRows: [["q1", "Ashby", "Python Developer", "junior", "site:jobs.ashbyhq.com python", "TRUE"], ["q2", "Lever", "Python Developer", "junior", "site:jobs.lever.co python", "TRUE"], ["q3", "Ashby", "Backend Developer", "junior", "disabled", "FALSE"]],
    ruleRows: [["Python signals", "python | django", "2"]]
  });
  assert.deepEqual(configuration.queries.map((query) => query.id), ["q1"]);
  assert.deepEqual(configuration.queries[0].allowedHosts, ["jobs.ashbyhq.com"]);
  assert.deepEqual(configuration.signalRules.python, ["python", "django"]);
});
