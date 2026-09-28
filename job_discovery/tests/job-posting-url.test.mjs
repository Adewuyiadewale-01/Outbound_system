import test from "node:test";
import assert from "node:assert/strict";
import { isCareerLandingPageUrl } from "../src/job-posting-url.mjs";

test("distinguishes Ashby company boards from individual job posts", () => {
  assert.equal(isCareerLandingPageUrl("https://jobs.ashbyhq.com/cradlebio", "Ashby"), true);
  assert.equal(isCareerLandingPageUrl("https://jobs.ashbyhq.com/tyba/6bf9a202-0b83-44c8-b06f-8f258fa8057e", "Ashby"), false);
});
