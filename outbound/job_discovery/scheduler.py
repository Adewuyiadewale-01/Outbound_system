"""Daily scheduler ported from ``job_discovery/src/scheduler.mjs``.

Due decisions run in the configured IANA timezone; a missed scheduled minute
still runs later the same day. ``start_scheduler`` returns a handle backed by a
daemon thread (used by the ``schedule`` CLI command); ``run_if_due`` is the
unit-tested core.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from outbound.job_discovery.runner import run_discovery


def zoned_parts(value: datetime, timezone_name: str) -> dict:
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    local = moment.astimezone(ZoneInfo(timezone_name))
    return {"dateKey": local.strftime("%Y-%m-%d"), "time": f"{local.hour:02d}:{local.minute:02d}"}


def scheduled_moment(settings: dict, date: datetime | None = None) -> dict:
    """Port of ``scheduledMoment``: local date key + HH:MM in the run timezone."""
    moment = date if date is not None else datetime.now(timezone.utc)
    parts = zoned_parts(moment, settings.get("timezone"))
    return {"dateKey": parts["dateKey"], "time": parts["time"]}


def minute_of_day(value: object) -> int:
    match = re.match(r"^(\d{1,2}):(\d{2})", str(value or ""))
    if not match:
        raise RuntimeError(f"Daily Run Time must use HH:MM format; received {value}")
    return int(match.group(1)) * 60 + int(match.group(2))


def run_if_due(
    *,
    settings: dict,
    state_store,
    dependencies: dict | None = None,
    get_dependencies=None,
    date: datetime | None = None,
) -> dict:
    """Port of ``runIfDue`` (automation gate; same-day missed runs execute)."""
    if not settings.get("automationEnabled"):
        return {"started": False, "reason": "automation_disabled"}
    moment = scheduled_moment(settings, date)
    if minute_of_day(moment["time"]) < minute_of_day(settings.get("dailyRunTime")):
        return {"started": False, "reason": "not_due"}
    state = state_store.read()
    if state.get("lastScheduledDate") == moment["dateKey"] or state.get("activeRunId"):
        return {"started": False, "reason": "already_started"}
    active_dependencies = get_dependencies() if get_dependencies else dependencies
    run = run_discovery(
        trigger="scheduled",
        state_store=state_store,
        settings=settings,
        **(active_dependencies or {}),
    )
    updated = state_store.read()
    updated["lastScheduledDate"] = moment["dateKey"]
    state_store.write(updated)
    return {"started": True, "run": run}


class SchedulerHandle:
    """Handle for the background scheduler thread."""

    def __init__(self, stop_event: threading.Event) -> None:
        self._stop_event = stop_event
        self.thread: threading.Thread | None = None

    def stop(self) -> None:
        self._stop_event.set()


def _log(logger, message: str, *, error: bool = False) -> None:
    if logger is None:
        print(message, file=sys.stderr if error else sys.stdout)
        return
    handler = getattr(logger, "error" if error else "log", None)
    if handler is None and callable(logger):
        handler = logger
    if handler:
        handler(message)


def start_scheduler(
    *,
    get_settings,
    get_dependencies,
    state_store,
    dependencies: dict | None = None,
    logger=None,
    interval_ms: int = 60_000,
    retry_delay_ms: int = 15 * 60_000,
) -> SchedulerHandle:
    """Port of ``startScheduler``: ticks every ``interval_ms`` with a 15-min backoff."""
    stop_event = threading.Event()
    handle = SchedulerHandle(stop_event)
    tick_state = {"running": False, "retry_after": 0.0}

    def tick() -> None:
        if tick_state["running"] or time.time() * 1000 < tick_state["retry_after"]:
            return
        tick_state["running"] = True
        try:
            settings = get_settings()
            result = run_if_due(
                settings=settings,
                state_store=state_store,
                dependencies=dependencies,
                get_dependencies=get_dependencies,
            )
            if result.get("started"):
                _log(
                    logger,
                    f"Scheduled run {result['run']['id']} completed with {result['run']['status']}.",
                )
            tick_state["retry_after"] = 0
        except Exception as error:  # noqa: BLE001 - mirrors the reference catch-all
            tick_state["retry_after"] = time.time() * 1000 + retry_delay_ms
            _log(
                logger,
                f"Scheduler error: {error}. Next attempt will wait {round(retry_delay_ms / 60_000)} minutes.",
                error=True,
            )
        finally:
            tick_state["running"] = False

    def loop() -> None:
        tick()
        while not stop_event.wait(interval_ms / 1000):
            tick()

    handle.thread = threading.Thread(target=loop, daemon=True)
    handle.thread.start()
    return handle
