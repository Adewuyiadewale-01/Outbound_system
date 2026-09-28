import { runDiscovery } from "./run.mjs";

function zonedParts(date, timezone) {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(date);
  return Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
}

export function scheduledMoment(settings, date = new Date()) {
  const parts = zonedParts(date, settings.timezone);
  return { dateKey: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}` };
}

function minuteOfDay(value) {
  const match = String(value || "").match(/^(\d{1,2}):(\d{2})/);
  if (!match) throw new Error(`Daily Run Time must use HH:MM format; received ${value}`);
  return Number(match[1]) * 60 + Number(match[2]);
}

export async function runIfDue({ settings, stateStore, dependencies, getDependencies, date = new Date() }) {
  if (!settings.automationEnabled) return { started: false, reason: "automation_disabled" };
  const moment = scheduledMoment(settings, date);
  if (minuteOfDay(moment.time) < minuteOfDay(settings.dailyRunTime)) return { started: false, reason: "not_due" };
  const state = await stateStore.read();
  if (state.lastScheduledDate === moment.dateKey || state.activeRunId) return { started: false, reason: "already_started" };
  const activeDependencies = getDependencies ? await getDependencies() : dependencies;
  const run = await runDiscovery({ ...activeDependencies, trigger: "scheduled", stateStore, settings });
  const updated = await stateStore.read(); updated.lastScheduledDate = moment.dateKey; await stateStore.write(updated);
  return { started: true, run };
}

export function startScheduler({ getSettings, getDependencies, stateStore, dependencies, logger = console, intervalMs = 60_000, retryDelayMs = 15 * 60_000 }) {
  let tickRunning = false;
  let retryAfter = 0;
  const tick = async () => {
    if (tickRunning || Date.now() < retryAfter) return;
    tickRunning = true;
    try {
      const settings = await getSettings();
      const result = await runIfDue({ settings, stateStore, dependencies, getDependencies });
      if (result.started) logger.log(`Scheduled run ${result.run.id} completed with ${result.run.status}.`);
      retryAfter = 0;
    } catch (error) {
      retryAfter = Date.now() + retryDelayMs;
      logger.error(`Scheduler error: ${error.message}. Next attempt will wait ${Math.round(retryDelayMs / 60_000)} minutes.`);
    } finally { tickRunning = false; }
  };
  void tick();
  return setInterval(tick, intervalMs);
}
