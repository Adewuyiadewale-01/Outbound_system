import test from "node:test";
import assert from "node:assert/strict";
import { verifySignals } from "../src/signals.mjs";

test("verifies the three strong signals", () => {
  const result = verifySignals({ title: "Junior Python Developer", description: "This fully remote team needs solid Python experience.", role: "Python Developer" });
  assert.equal(result.juniorStatus, "verified");
  assert.equal(result.remoteStatus, "verified");
  assert.equal(result.pythonStatus, "verified");
});

test("sends conflicting seniority wording to review", () => {
  const result = verifySignals({ title: "Junior / Senior Engineer", description: "A role with conflicting level wording.", role: "Software Engineer" });
  assert.equal(result.juniorStatus, "conflicting");
  assert.equal(result.reviewReason, "Conflicting signals");
});

test("records unambiguous hybrid and onsite location evidence separately from remote", () => {
  const hybrid = verifySignals({ title: "Python Developer", description: "This role follows a hybrid work model.", role: "Python Developer" });
  const onsite = verifySignals({ title: "Python Developer", description: "This is an on-site role in Lagos.", role: "Python Developer" });
  assert.equal(hybrid.remoteStatus, "hybrid_verified");
  assert.equal(onsite.remoteStatus, "onsite_verified");
});

test("sends mixed remote and hybrid job-page evidence to review", () => {
  const result = verifySignals({
    title: "Senior Product Software Engineer (Python)",
    location: "SF Bay Area, Chicago, LA or Remote",
    description: "Half of the company is remote. Flexible Work Environment: Hybrid work model, remote work options.",
    role: "Python Developer"
  });
  assert.equal(result.remoteStatus, "conflicting");
  assert.equal(result.reviewReason, "Conflicting signals");
});
