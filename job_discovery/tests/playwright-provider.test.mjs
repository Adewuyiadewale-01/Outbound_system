import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { nextPaginationState, parsePlaywrightResult, PlaywrightBrowser, PlaywrightGoogleSearchProvider, SearchBlockedError } from "../src/playwright-provider.mjs";

test("reads JSON payloads emitted by playwright-cli eval", () => {
  assert.deepEqual(parsePlaywrightResult('### Result\n[{"title":"Junior role"}]\n### Page'), [{ title: "Junior role" }]);
});

test("opens a headed browser with a persistent local profile", async () => {
  const browser = new PlaywrightBrowser({ profilePath: "./data/test-browser-profile", headed: true });
  let args;
  browser.command = async (received) => { args = received; };
  await browser.open("https://example.com");
  assert.deepEqual(args, ["open", "https://example.com", "--headed", "--persistent", "--profile", path.resolve("./data/test-browser-profile")]);
});

test("paginates until two ATS-valid thin pages and rejects off-domain results", async () => {
  const pages = [
    { hasNext: true, results: [1, 2, 3].map((number) => ({ title: `Job ${number}`, link: `https://jobs.ashbyhq.com/acme/job-${number}`, snippet: "Remote" })).concat({ title: "Noise", link: "https://example.com/noise", snippet: "" }) },
    { hasNext: true, results: [4, 5].map((number) => ({ title: `Job ${number}`, link: `https://jobs.ashbyhq.com/acme/job-${number}`, snippet: "Remote" })) },
    { hasNext: true, results: [{ title: "Job 6", link: "https://jobs.ashbyhq.com/acme/job-6", snippet: "Remote" }] }
  ];
  const browser = { open: async () => {}, rejectGoogleCookiesIfPresent: async () => {}, evaluate: async () => ({ url: "https://google.com/search", title: "Search", bodyText: "", ...pages.shift() }), close: async () => {} };
  const provider = new PlaywrightGoogleSearchProvider({ browser });
  const results = await provider.search({ query: "site:jobs.ashbyhq.com python", allowedHosts: ["jobs.ashbyhq.com"] }, { maxPages: 10, minPageDelayMs: 0, maxPageDelayMs: 0 });
  assert.equal(results.length, 6);
  assert.equal(results.paginationStop, "low_yield");
});

test("continues from a saved page checkpoint when the total page ceiling is disabled", async () => {
  const opened = [];
  const browser = {
    open: async (url) => { opened.push(url); },
    rejectGoogleCookiesIfPresent: async () => {},
    evaluate: async () => ({
      url: "https://google.com/search?start=200", title: "Search", bodyText: "", hasNext: false,
      results: [{ title: "Job", link: "https://jobs.ashbyhq.com/acme/page-21", snippet: "Remote" }]
    }),
    close: async () => {}
  };
  const provider = new PlaywrightGoogleSearchProvider({ browser });
  const results = await provider.search(
    { query: "site:jobs.ashbyhq.com backend", allowedHosts: ["jobs.ashbyhq.com"] },
    { resume: { nextPage: 20 }, maxPages: 0, minPageDelayMs: 0, maxPageDelayMs: 0 }
  );
  assert.match(opened[0], /start=200/);
  assert.equal(results.length, 1);
  assert.equal(results.paginationStop, "exhausted");
});

test("normalizes tracking variants before recording search results", async () => {
  const browser = {
    open: async () => {},
    rejectGoogleCookiesIfPresent: async () => {},
    evaluate: async () => ({
      url: "https://google.com/search", title: "Search", bodyText: "", hasNext: false,
      results: [
        { title: "Job", link: "https://jobs.ashbyhq.com/acme/role-1?utm_source=google", snippet: "Remote" },
        { title: "Job", link: "https://jobs.ashbyhq.com/acme/role-1?ref=duplicate", snippet: "Remote" }
      ]
    }),
    close: async () => {}
  };
  const provider = new PlaywrightGoogleSearchProvider({ browser });
  const results = await provider.search({ query: "site:jobs.ashbyhq.com python", allowedHosts: ["jobs.ashbyhq.com"] });
  assert.equal(results.length, 1);
  assert.equal(results[0].link, "https://jobs.ashbyhq.com/acme/role-1");
});

test("leaves a query retryable when Google presents a challenge", async () => {
  const browser = { open: async () => {}, rejectGoogleCookiesIfPresent: async () => {}, evaluate: async () => ({ url: "https://google.com/sorry/", title: "Verify", bodyText: "Our systems detected unusual traffic", hasNext: false, results: [] }) };
  const provider = new PlaywrightGoogleSearchProvider({ browser });
  await assert.rejects(() => provider.search({ query: "site:jobs.ashbyhq.com python" }), SearchBlockedError);
});

test("ends pagination after two consecutive low-yield pages", () => {
  const first = nextPaginationState(0, 2);
  assert.deepEqual(first, { thinPages: 1, complete: false });
  assert.deepEqual(nextPaginationState(first.thinPages, 1), { thinPages: 2, complete: true });
  assert.deepEqual(nextPaginationState(1, 3), { thinPages: 0, complete: false });
});
