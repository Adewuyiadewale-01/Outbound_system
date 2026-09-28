import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const label = "com.fulltime-job.daily-job-discovery";
const project = fileURLToPath(new URL("..", import.meta.url));
const launchAgents = path.join(os.homedir(), "Library", "LaunchAgents");
const plistPath = path.join(launchAgents, `${label}.plist`);
const escapeXml = (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const uid = process.getuid();
let nodePath = "/opt/homebrew/bin/node";
try { await fs.access(nodePath); } catch { nodePath = process.execPath; }

await fs.mkdir(launchAgents, { recursive: true });
await fs.mkdir(path.join(project, "data"), { recursive: true });
const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key><array><string>${escapeXml(nodePath)}</string><string>${escapeXml(path.join(project, "src", "cli.mjs"))}</string><string>schedule</string></array>
  <key>WorkingDirectory</key><string>${escapeXml(project)}</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>${escapeXml(process.env.PATH || "/usr/local/bin:/usr/bin:/bin")}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>${escapeXml(path.join(project, "data", "scheduler.log"))}</string>
  <key>StandardErrorPath</key><string>${escapeXml(path.join(project, "data", "scheduler-error.log"))}</string>
</dict></plist>
`;
const temporary = `${plistPath}.tmp`;
await fs.writeFile(temporary, plist);
await fs.rename(temporary, plistPath);
spawnSync("launchctl", ["bootout", `gui/${uid}`, plistPath], { stdio: "ignore" });
const result = spawnSync("launchctl", ["bootstrap", `gui/${uid}`, plistPath], { encoding: "utf8" });
if (result.status !== 0) throw new Error(result.stderr || `launchctl bootstrap failed with ${result.status}`);
console.log(`Installed and started ${label}. Daily execution remains governed by config/runtime.json → automationEnabled.`);
