import crypto from "node:crypto";

const trackingKeys = new Set(["gclid", "fbclid", "ref", "source", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"]);

export function canonicalizeUrl(value) {
  const url = new URL(value);
  url.hash = "";
  url.hostname = url.hostname.toLowerCase();
  for (const key of [...url.searchParams.keys()]) {
    if (trackingKeys.has(key.toLowerCase()) || key.toLowerCase().startsWith("utm_")) url.searchParams.delete(key);
  }
  url.pathname = url.pathname.replace(/\/+$/, "") || "/";
  return url.toString();
}

export function stableHash(value) {
  return crypto.createHash("sha256").update(value).digest("hex").slice(0, 24);
}

export function normalizedText(value = "") {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim().replace(/\s+/g, " ");
}

export function findAtsJobId(url) {
  const { hostname, pathname, searchParams } = new URL(url);
  const explicit = ["jobId", "job_id", "gh_jid", "lever-origin", "opening", "requisitionId"]
    .map((key) => searchParams.get(key)).find(Boolean);
  const meaningfulPath = pathname.split("/").filter(Boolean).at(-1);
  return explicit || (meaningfulPath && meaningfulPath.length >= 5 ? `${hostname}:${meaningfulPath}` : undefined);
}
