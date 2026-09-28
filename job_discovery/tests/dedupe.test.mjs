import test from "node:test";
import assert from "node:assert/strict";
import { dedupeCandidates, toCandidate } from "../src/dedupe.mjs";

const query = { id: "Ashby:Python Developer:junior", platform: "Ashby", role: "Python Developer", type: "junior" };

test("normalizes tracking URLs and merges same-run query provenance before hydration", () => {
  const first = toCandidate({ title: "Junior Python Developer", link: "https://jobs.ashbyhq.com/acme/abcdefgh?utm_source=google", snippet: "Remote" }, query);
  const second = toCandidate({ title: "Junior Python Developer", link: "https://jobs.ashbyhq.com/acme/abcdefgh?ref=search", snippet: "Remote" }, { ...query, id: "Ashby:Python Developer:junior-signal" });
  const result = dedupeCandidates([first, second]);
  assert.equal(result.uniqueCandidates.length, 1);
  assert.deepEqual(result.uniqueCandidates[0].sourceQueries.sort(), [query.id, "Ashby:Python Developer:junior-signal"].sort());
  assert.equal(result.hydrationQueue.length, 1);
});

test("skips verified unchanged records before listing verification", () => {
  const candidate = toCandidate({ title: "Junior Python Developer", link: "https://jobs.ashbyhq.com/acme/abcdefgh", snippet: "Remote" }, query);
  const known = { [candidate.jobId]: { ...candidate, juniorStatus: "verified", remoteStatus: "verified", pythonStatus: "verified" } };
  assert.equal(dedupeCandidates([candidate], known).hydrationQueue.length, 0);
});
