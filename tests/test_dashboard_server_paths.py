"""Guard the dashboard server's repo-root resolution against carve regressions."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER = REPO_ROOT / "orchestrator" / "dashboard" / "server" / "local_server.py"


def test_project_dir_resolves_to_repo_root() -> None:
    source = SERVER.read_text()
    match = re.search(r"PROJECT_DIR = APP_DIR\.parents\[(\d+)\]", source)
    assert match, "PROJECT_DIR definition not found in local_server.py"
    parents_index = int(match.group(1))
    app_dir = SERVER.resolve().parent
    project_dir = app_dir.parents[parents_index]
    assert project_dir == REPO_ROOT
    assert (project_dir / "scripts" / "post_engagement.py").is_file()
    assert (project_dir / "helpers").is_dir()
