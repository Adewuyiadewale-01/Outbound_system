import { canonicalizeUrl, findAtsJobId, normalizedText, stableHash } from "./url.mjs";

export function toCandidate(result, query) {
  const canonicalUrl = canonicalizeUrl(result.link);
  const jobId = findAtsJobId(canonicalUrl);
  const title = result.title || "Untitled job";
  const snippet = result.snippet || "";
  const fallback = `${query.platform}|${normalizedText(result.company || "")}|${normalizedText(title)}|${normalizedText(result.location || "")}`;
  const identity = jobId ? `ats:${jobId}` : `url:${canonicalUrl}`;
  return {
    jobId: stableHash(identity), canonicalUrl, atsJobId: jobId, title, snippet,
    displayLink: result.displayLink || new URL(canonicalUrl).hostname,
    platform: query.platform, role: query.role, field: query.field === "design" ? "design" : "engineering", sourceQueries: [query.id],
    queryType: query.type, resultHash: stableHash(`${title}|${snippet}|${canonicalUrl}`),
    fallbackFingerprint: stableHash(fallback)
  };
}

export function dedupeCandidates(candidates, knownJobs = {}, { recheckAfterMs = Infinity, now = Date.now() } = {}) {
  const run = new Map();
  const hydrationQueue = [];
  for (const candidate of candidates) {
    const existingRun = run.get(candidate.jobId) || [...run.values()].find((item) => item.fallbackFingerprint === candidate.fallbackFingerprint);
    if (existingRun) {
      existingRun.sourceQueries = [...new Set([...existingRun.sourceQueries, ...candidate.sourceQueries])];
      continue;
    }
    run.set(candidate.jobId, candidate);
    const known = knownJobs[candidate.jobId] || Object.values(knownJobs).find((job) => job.fallbackFingerprint === candidate.fallbackFingerprint);
    const checkedAt = known?.verificationCheckedAt ? Date.parse(known.verificationCheckedAt) : 0;
    const recheckDue = Boolean(known) && (!Number.isFinite(checkedAt) || now - checkedAt >= recheckAfterMs);
    if (!known || known.resultHash !== candidate.resultHash || known.juniorStatus === "unsure" || known.remoteStatus === "unsure" || known.pythonStatus === "unsure" || recheckDue) {
      hydrationQueue.push(candidate);
    }
  }
  return { uniqueCandidates: [...run.values()], hydrationQueue };
}
