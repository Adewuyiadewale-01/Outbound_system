"""The legacy node service must not inherit the local port's env values."""

from __future__ import annotations

import os
from unittest.mock import patch

from orchestrator.dashboard.server import local_server

BLOCKED = {
    "APPS_SCRIPT_TOKEN",
    "GOOGLE_APPS_SCRIPT_URL",
    "LOCAL_CONTROL_FILE",
    "PLAYWRIGHT_HEADED",
    "PLAYWRIGHT_PROFILE_DIR",
    "SEARCH_PROVIDER",
    "SHEETS_TRANSPORT",
    "STATE_FILE",
}
SENTINELS = {key: "sentinel-value" for key in sorted(BLOCKED)}


def test_job_discovery_node_env_removes_port_keys() -> None:
    with patch.dict(os.environ, SENTINELS, clear=False):
        env = local_server.job_discovery_node_env()
    assert BLOCKED.isdisjoint(env)
    assert env.get("PATH") == os.environ.get("PATH")


def test_run_job_discovery_node_spawns_with_scrubbed_env() -> None:
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return type("Completed", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()

    with (
        patch.dict(os.environ, SENTINELS, clear=False),
        patch.object(local_server, "job_discovery_available", return_value=True),
        patch.object(local_server.subprocess, "run", side_effect=fake_run),
    ):
        result = local_server.run_job_discovery_node("src/cli.mjs", "status")
    assert result["ok"] is True
    assert captured["cmd"] == ["node", "src/cli.mjs", "status"]
    assert captured["env"] is not None
    assert BLOCKED.isdisjoint(captured["env"])
    assert captured["env"].get("PATH") == os.environ.get("PATH")


def test_start_job_discovery_action_spawns_with_scrubbed_env() -> None:
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return None

    with (
        patch.dict(os.environ, SENTINELS, clear=False),
        patch.object(local_server, "job_discovery_available", return_value=True),
        patch.object(local_server, "read_json", return_value=None),
        patch.object(local_server.subprocess, "Popen", side_effect=fake_popen),
    ):
        result = local_server.start_job_discovery_action("run")
    assert result["ok"] is True
    assert captured["cmd"] == ["node", "src/cli.mjs", "run"]
    assert captured["env"] is not None
    assert BLOCKED.isdisjoint(captured["env"])
