import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { spawnSync } from "node:child_process";

const label = "com.fulltime-job.daily-job-discovery";
const plistPath = path.join(os.homedir(), "Library", "LaunchAgents", `${label}.plist`);
spawnSync("launchctl", ["bootout", `gui/${process.getuid()}`, plistPath], { stdio: "ignore" });
try { await fs.unlink(plistPath); } catch (error) { if (error.code !== "ENOENT") throw error; }
console.log(`Stopped and removed ${label}.`);
