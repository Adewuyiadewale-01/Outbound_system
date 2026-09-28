import { canonicalizeUrl } from "./url.mjs";

const stripTags = (value = "") => value.replace(/<script[\s\S]*?<\/script>/gi, " ").replace(/<style[\s\S]*?<\/style>/gi, " ").replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/\s+/g, " ").trim();

function extractJsonLd(html) {
  const blocks = [...html.matchAll(/<script[^>]+type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi)];
  for (const block of blocks) {
    try {
      const parsed = JSON.parse(block[1]);
      const graph = Array.isArray(parsed?.["@graph"]) ? parsed["@graph"] : [];
      const values = Array.isArray(parsed) ? parsed : [parsed, ...graph];
      const job = values.find((item) => item?.["@type"] === "JobPosting" || item?.["@type"]?.includes?.("JobPosting"));
      if (job) return job;
    } catch { /* Ignore malformed third-party schema. */ }
  }
  return undefined;
}

export async function readListing(candidate, { fetchImpl = fetch, timeoutMs = 15_000 } = {}) {
  const response = await fetchImpl(candidate.canonicalUrl, { signal: AbortSignal.timeout(timeoutMs), headers: { Accept: "text/html,application/xhtml+xml" } });
  if (!response.ok) throw new Error(`Listing returned HTTP ${response.status}`);
  const html = await response.text();
  const schema = extractJsonLd(html);
  const titleMatch = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  const title = schema?.title || candidate.title || stripTags(titleMatch?.[1]) || "Untitled job";
  const description = schema?.description ? stripTags(schema.description) : stripTags(html).slice(0, 35_000);
  const company = schema?.hiringOrganization?.name || candidate.displayLink.replace(/^www\./, "").split(".")[0];
  const location = schema?.jobLocation?.address?.addressLocality || schema?.applicantLocationRequirements?.name || "";
  return { title, description, company, location, canonicalUrl: canonicalizeUrl(response.url || candidate.canonicalUrl) };
}
