import fs from "node:fs/promises";
import { PlaywrightBrowser, PlaywrightGoogleSearchProvider, createPlaywrightListingReader } from "./playwright-provider.mjs";

export class FixtureSearchProvider {
  constructor(file = new URL("../fixtures/search-results.json", import.meta.url)) { this.file = file; }
  async search(query) {
    const fixture = JSON.parse(await fs.readFile(this.file, "utf8"));
    return fixture[query.id] || [];
  }
}

export class GoogleCustomSearchProvider {
  constructor({ apiKey, engineId, fetchImpl = fetch }) { this.apiKey = apiKey; this.engineId = engineId; this.fetchImpl = fetchImpl; }
  async search(query) {
    const url = new URL("https://customsearch.googleapis.com/customsearch/v1");
    url.searchParams.set("key", this.apiKey); url.searchParams.set("cx", this.engineId); url.searchParams.set("q", query.query); url.searchParams.set("num", "10");
    const response = await this.fetchImpl(url);
    if (!response.ok) throw new Error(`Search API returned HTTP ${response.status}`);
    const body = await response.json();
    return (body.items || []).map((item) => ({ title: item.title, link: item.link, snippet: item.snippet, displayLink: item.displayLink }));
  }
}

export class HttpSearchProvider {
  constructor({ endpoint, token, fetchImpl = fetch }) { this.endpoint = endpoint; this.token = token; this.fetchImpl = fetchImpl; }
  async search(query) {
    const response = await this.fetchImpl(this.endpoint, { method: "POST", headers: { "content-type": "application/json", ...(this.token ? { authorization: `Bearer ${this.token}` } : {}) }, body: JSON.stringify({ query: query.query, queryId: query.id }) });
    if (!response.ok) throw new Error(`Search provider returned HTTP ${response.status}`);
    const body = await response.json();
    if (!Array.isArray(body.results)) throw new Error("Search provider response must include a results array");
    return body.results;
  }
}

export function createSearchProvider(environment) {
  if (environment.SEARCH_PROVIDER === "google-cse") return new GoogleCustomSearchProvider({ apiKey: environment.GOOGLE_CSE_API_KEY, engineId: environment.GOOGLE_CSE_ID });
  if (environment.SEARCH_PROVIDER === "http") return new HttpSearchProvider({ endpoint: environment.SEARCH_API_URL, token: environment.SEARCH_API_TOKEN });
  if (environment.SEARCH_PROVIDER === "playwright-google") return new PlaywrightGoogleSearchProvider({ browser: new PlaywrightBrowser({ headed: environment.PLAYWRIGHT_HEADED !== "false", profilePath: environment.PLAYWRIGHT_PROFILE_DIR }) });
  return new FixtureSearchProvider();
}

export function createListingReader(environment, searchProvider) {
  if (environment.SEARCH_PROVIDER === "playwright-google" && searchProvider.browser) return createPlaywrightListingReader(searchProvider.browser);
  return undefined;
}
