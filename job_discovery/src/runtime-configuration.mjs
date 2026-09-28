import { platforms as defaultPlatforms, roles as defaultRoles } from "../config/defaults.mjs";
import { buildQueries } from "./query-builder.mjs";

const enabled = (value, fallback = true) => value === undefined || value === "" ? fallback : !/^(false|no|0|disabled)$/i.test(String(value).trim());
const vocabulary = (value = "") => String(value).split("|").map((item) => item.trim()).filter(Boolean);
export const hostsFromSiteTarget = (target = "") => [...String(target).matchAll(/site:([a-z0-9.-]+)/gi)].map((match) => match[1].toLowerCase());
const fieldForRole = (name) => defaultRoles.find((role) => role.name === name)?.field === "design" ? "design" : "engineering";

export function configurationFromRows(configuration = {}) {
  const platformRows = configuration.platformRows || [];
  const roleRows = configuration.roleRows || [];
  const queryRows = configuration.queryRows || [];
  const ruleRows = configuration.ruleRows || [];
  const platforms = platformRows.length ? platformRows.filter((row) => row[0]).map((row) => ({ name: row[0], siteTarget: row[1] || "", enabled: enabled(row[2]), allowedHosts: hostsFromSiteTarget(row[1]) })) : defaultPlatforms.map((platform) => ({ ...platform, allowedHosts: hostsFromSiteTarget(platform.siteTarget) }));
  const roleMap = new Map();
  for (const row of roleRows.filter((item) => item[0])) {
    const role = roleMap.get(row[0]) || { name: row[0], field: fieldForRole(row[0]), junior: [], unfiltered: [], strongSignals: [] };
    if (row[1] === "junior") role.junior.push(...vocabulary(row[2]));
    if (row[1] === "unfiltered") role.unfiltered.push(...vocabulary(row[2]));
    roleMap.set(role.name, role);
  }
  const roles = roleMap.size ? [...roleMap.values()] : defaultRoles;
  const platformMap = new Map(platforms.map((platform) => [platform.name, platform]));
  const queries = queryRows.length ? queryRows.filter((row) => row[0] && enabled(row[5]) && platformMap.get(row[1])?.enabled !== false).map((row) => ({
    id: row[0], platform: row[1], role: row[2], field: fieldForRole(row[2]), type: row[3], query: row[4], allowedHosts: platformMap.get(row[1])?.allowedHosts || []
  })) : buildQueries({ activePlatforms: platforms, activeRoles: roles });
  const rules = Object.fromEntries(ruleRows.filter((row) => row[0]).map((row) => [String(row[0]).toLowerCase(), vocabulary(row[1])]));
  return { platforms, roles, queries, signalRules: { junior: rules["junior signals"], senior: rules["senior signals"], remote: rules["remote signals"], hybrid: rules["hybrid signals"], onsite: rules["onsite signals"], nonRemote: rules["non-remote signals"], python: rules["python signals"] } };
}
