const juniorPatterns = [/\bjunior\b/i, /\bjr\.?\b/i, /\bassociate\b/i, /\bentry[ -]level\b/i, /\bnew grad\b/i, /\bgraduate\b/i, /\b(?:engineer|developer)\s+(?:i|1)\b/i, /\bearly career\b/i];
const seniorPatterns = [/\bsenior\b/i, /\bstaff\b/i, /\bprincipal\b/i, /\blead\b/i, /\bmanager\b/i, /\bdirector\b/i];
const remotePatterns = [/\bremote\b/i, /\bwork from home\b/i, /\bdistributed\b/i, /\banywhere\b/i];
const hybridPatterns = [/\bhybrid\b/i];
const onsitePatterns = [/\bno remote\b/i, /\bon[ -]site\b/i, /\bin office\b/i];
const pythonPatterns = [/\bpython\b/i];

const escape = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
function patternsFromTerms(terms, fallback, { levelOne = false } = {}) {
  if (!Array.isArray(terms) || !terms.length) return fallback;
  const patterns = [];
  for (const raw of terms) {
    const term = String(raw).trim();
    if (!term) continue;
    if (levelOne && /^(i|1)$/i.test(term)) { patterns.push(/\b(?:engineer|developer)\s+(?:i|1)\b/i); continue; }
    const body = escape(term).replace(/\\ /g, "[ -]");
    patterns.push(new RegExp(`\\b${body}\\b`, "i"));
  }
  return patterns.length ? patterns : fallback;
}

function findEvidence(text, patterns) {
  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (match) return match[0];
  }
  return undefined;
}

function compactEvidence(text, term) {
  if (!term) return "";
  const index = text.toLowerCase().indexOf(term.toLowerCase());
  return text.slice(Math.max(0, index - 90), index + term.length + 140).replace(/\s+/g, " ").trim();
}

export function verifySignals({ title = "", description = "", location = "", role = "" }, rules = {}) {
  const activeJuniorPatterns = patternsFromTerms(rules.junior, juniorPatterns, { levelOne: true });
  const activeSeniorPatterns = patternsFromTerms(rules.senior, seniorPatterns);
  const activeRemotePatterns = patternsFromTerms(rules.remote, remotePatterns);
  // Older configurations used one non-remote rule. Keep those terms working,
  // but split hybrid from onsite so the Sheet reports the stronger outcome.
  const legacyNonRemoteTerms = Array.isArray(rules.nonRemote) ? rules.nonRemote : [];
  const activeHybridPatterns = [
    ...patternsFromTerms(rules.hybrid, hybridPatterns),
    ...patternsFromTerms(legacyNonRemoteTerms.filter((term) => /hybrid/i.test(term)), [])
  ];
  const activeOnsitePatterns = [
    ...patternsFromTerms(rules.onsite, onsitePatterns),
    ...patternsFromTerms(legacyNonRemoteTerms.filter((term) => !/hybrid/i.test(term)), [])
  ];
  const activePythonPatterns = patternsFromTerms(rules.python, pythonPatterns);
  const text = `${title}\n${location}\n${description}`;
  const titleJunior = findEvidence(title, activeJuniorPatterns);
  const titleSenior = findEvidence(title, activeSeniorPatterns);
  const junior = titleJunior || findEvidence(description, activeJuniorPatterns);
  // Seniority is only conclusive when it qualifies the job title. Mentions of a
  // senior colleague in the description must not downgrade a junior opening.
  const senior = titleSenior;
  const remote = findEvidence(text, activeRemotePatterns);
  const hybrid = findEvidence(text, activeHybridPatterns);
  const onsite = findEvidence(text, activeOnsitePatterns);
  const python = findEvidence(text, activePythonPatterns);
  const titleAppearsPartial = title.trim().length < 5 || /untitled job/i.test(title);

  const juniorStatus = titleJunior && titleSenior ? "conflicting" : junior ? "verified" : senior ? "senior_verified" : titleAppearsPartial ? "unsure" : "not_found";
  // A job cannot be reported as remote-only when the same page explicitly says
  // hybrid or onsite. Mixed wording is retained for review rather than guessed.
  const remoteStatus = remote && (hybrid || onsite) ? "conflicting"
    : hybrid ? "hybrid_verified"
      : onsite ? "onsite_verified"
        : remote ? "verified"
          : titleAppearsPartial ? "unsure" : "not_found";
  const pythonRequired = /python/i.test(role);
  const pythonStatus = python ? "verified" : pythonRequired && titleAppearsPartial ? "unsure" : "not_found";
  const verifiedSignals = [juniorStatus === "verified", remoteStatus === "verified", pythonStatus === "verified"].filter(Boolean).length;

  return {
    juniorStatus, remoteStatus, pythonStatus,
    evidenceText: [junior || senior, remote, hybrid || onsite, python].filter(Boolean).map((term) => compactEvidence(text, term)).join(" | "),
    score: verifiedSignals * 25 + (juniorStatus === "verified" ? 20 : 0) + (remoteStatus === "verified" ? 15 : 0),
    reviewReason: [juniorStatus, remoteStatus, pythonStatus].includes("conflicting") ? "Conflicting signals" : [juniorStatus, remoteStatus, pythonStatus].includes("unsure") ? "Signal could not be confirmed" : ""
  };
}
