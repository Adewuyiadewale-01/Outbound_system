export function isCareerLandingPageUrl(value, platform = "") {
  let url;
  try { url = new URL(value); } catch { return false; }
  const segments = url.pathname.split("/").filter(Boolean);
  // Ashby company boards use /{company}; individual posts use /{company}/{job-id}.
  // This check is intentionally narrow so unknown URL shapes on other ATS platforms
  // remain eligible for the page-level JobPosting validation.
  return /ashbyhq\.com$/i.test(url.hostname) && (platform === "Ashby" || !platform) && segments.length < 2;
}

export function isJobPostingCandidate(candidate) {
  return !isCareerLandingPageUrl(candidate.canonicalUrl, candidate.platform);
}
