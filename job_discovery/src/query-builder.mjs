import { platforms, roles } from "../config/defaults.mjs";

const signals = ["junior", "\"entry level\"", "\"new grad\"", "associate", "\"early career\""];
const quoted = (values, titleOnly = true) => values.map((value) => `${titleOnly ? "intitle:" : ""}\"${value}\"`).join(" OR ");
const hostsFromTarget = (target = "") => [...target.matchAll(/site:([a-z0-9.-]+)/gi)].map((match) => match[1].toLowerCase());
const roleField = (role) => role.field === "design" ? "design" : "engineering";

function alternateFields(queries) {
  const buckets = new Map([["engineering", []], ["design", []]]);
  for (const query of queries) buckets.get(roleField(query)).push(query);
  const ordered = [];
  while (buckets.get("engineering").length || buckets.get("design").length) {
    for (const field of ["engineering", "design"]) {
      const query = buckets.get(field).shift();
      if (query) ordered.push(query);
    }
  }
  return ordered;
}

export function buildQueries({ activePlatforms = platforms, activeRoles = roles } = {}) {
  const queries = [];
  for (const platform of activePlatforms.filter((item) => item.enabled)) {
    for (const role of activeRoles) {
      queries.push({
        id: `${platform.name}:${role.name}:junior`, platform: platform.name, role: role.name, field: roleField(role), type: "junior",
        query: `${platform.siteTarget} (${quoted(role.junior)}) remote -\"no remote\" -intitle:senior -intitle:staff -intitle:principal`, allowedHosts: platform.allowedHosts || hostsFromTarget(platform.siteTarget)
      });
      queries.push({
        id: `${platform.name}:${role.name}:unfiltered`, platform: platform.name, role: role.name, field: roleField(role), type: "unfiltered",
        query: `${platform.siteTarget} (${quoted(role.unfiltered)}) remote -\"no remote\"`, allowedHosts: platform.allowedHosts || hostsFromTarget(platform.siteTarget)
      });
      queries.push({
        id: `${platform.name}:${role.name}:junior-signal`, platform: platform.name, role: role.name, field: roleField(role), type: "junior-signal",
        query: `${platform.siteTarget} (${quoted(role.unfiltered, false)}) (${signals.join(" OR ")}) remote -\"no remote\"`, allowedHosts: platform.allowedHosts || hostsFromTarget(platform.siteTarget)
      });
    }
  }
  return alternateFields(queries);
}
