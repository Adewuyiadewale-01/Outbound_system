"""CLI, argument parsing, and the built-in self-test harness.

Extracted from scripts/check_prefinal_activity.py during the activity_check
carve (see docs/CARVE-ACTIVITY-CHECK.md, slice S14). Pure move.
"""

import argparse
import json
import os
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from outbound.activity_check.analysis import (
    activity_detail_empty_success_reason,
    activity_level,
    rank_row_for_final,
)
from outbound.activity_check.config import (
    DEFAULT_ACTIVITY_SEQUENCE_TAB,
    DEFAULT_CREDS,
    DEFAULT_FINAL_TAB,
    DEFAULT_LEADS_SHEET_URL,
    DEFAULT_MAX_TARGET_ATTEMPTS,
    DEFAULT_PREFINAL_TAB,
)
from outbound.activity_check.finalize import final_source_row_count, ready_final_rows
from outbound.activity_check.reader import LiveActivityReader
from outbound.activity_check.runner import run
from outbound.activity_check.sequence import distribute_slots_to_batches
from outbound.activity_check.targets import extract_targets
from outbound.activity_check.text import (
    canonical_linkedin_profile_url,
    navigation_type_key,
    normalize_navigation_type,
)
from outbound.outreach.paths import OBF_SHEET_URL


def fake_activity(
    *, posts: Sequence[str] = (), comments: Sequence[str] = (), reactions: Sequence[str] = ()
) -> dict[str, Any]:
    return {
        "tabs": {
            "posts": {"activities": [{"time_text": value} for value in posts]},
            "comments": {"activities": [{"time_text": value} for value in comments]},
            "reactions": {"activities": [{"time_text": value} for value in reactions]},
        }
    }


def run_self_tests() -> None:
    row = {
        "ID": "1",
        "Company": "Example",
        "Website": "https://example.com",
        "Company LinkedIn": "",
        "Emp Count": "10",
        "Source Tab": "",
        "Use": "yes",
        "P1 Name": "One",
        "P1 Title": "CEO",
        "P1 LinkedIn": "https://www.linkedin.com/in/one",
        "P1 Email": "",
        "P2 Name": "Two",
        "P2 Title": "COO",
        "P2 LinkedIn": "https://www.linkedin.com/in/two",
        "P2 Email": "",
        "P3 Name": "Three",
        "P3 LinkedIn": "https://www.linkedin.com/in/three",
        "_row_number": 2,
    }
    targets = extract_targets([row])
    assert [target["prefix"] for target in targets] == ["P1", "P2"]
    locale_row = {
        **row,
        "ID": "2",
        "P1 LinkedIn": "https://nl.linkedin.com/in/example-person/nl?trk=test",
        "P2 LinkedIn": "",
    }
    locale_targets = extract_targets([locale_row])
    assert len(locale_targets) == 1
    assert locale_targets[0]["profile_url"] == "https://www.linkedin.com/in/example-person"
    assert (
        locale_targets[0]["original_profile_url"]
        == "https://nl.linkedin.com/in/example-person/nl?trk=test"
    )
    assert locale_targets[0]["profile_url_normalized"] is True
    assert activity_level(fake_activity(posts=["6d"])) == "Very active"
    assert activity_level(fake_activity(comments=["1d", "6d"])) == "Very active"
    assert activity_level(fake_activity(reactions=["1d", "6d"])) == "Very active"
    assert activity_level(fake_activity(posts=["13d"])) == "Active"
    assert (
        activity_level(fake_activity(comments=["20d", "21d", "22d"], reactions=["23d", "24d"]))
        == "Active"
    )
    assert activity_level(fake_activity(comments=["300d"])) == "Not active"
    proven_recent_with_uncertain_tab = fake_activity(posts=["6d"])
    proven_recent_with_uncertain_tab["tabs"]["comments"]["activity_classification_uncertain"] = True
    assert activity_level(proven_recent_with_uncertain_tab) == "Very active"
    proven_active_with_uncertain_tab = fake_activity(
        comments=["20d", "21d", "22d"], reactions=["23d", "24d"]
    )
    proven_active_with_uncertain_tab["tabs"]["posts"]["activity_classification_uncertain"] = True
    assert activity_level(proven_active_with_uncertain_tab) == "Active"
    old_only_with_uncertain_tab = fake_activity(comments=["300d"])
    old_only_with_uncertain_tab["tabs"]["reactions"]["activity_classification_uncertain"] = True
    assert activity_level(old_only_with_uncertain_tab) == ""
    assert (
        activity_detail_empty_success_reason(old_only_with_uncertain_tab)
        == "activity_classification_uncertain"
    )
    canonical_example = "https://www.linkedin.com/in/example-person"
    assert (
        canonical_linkedin_profile_url("https://nl.linkedin.com/in/example-person/nl?trk=test")
        == canonical_example
    )
    assert (
        canonical_linkedin_profile_url("https://www.nl.linkedin.com/in/example-person/")
        == canonical_example
    )
    assert canonical_linkedin_profile_url("linkedin.com/in/example-person") == canonical_example
    assert (
        canonical_linkedin_profile_url("https://rs.linkedin.com/in/example-person#:~:text=Example")
        == canonical_example
    )
    assert (
        canonical_linkedin_profile_url(
            "https://www.linkedin.com/in/example-person/recent-activity/reactions/"
        )
        == canonical_example
    )
    assert canonical_linkedin_profile_url("https://example.com/in/example-person") == ""
    assert canonical_linkedin_profile_url("https://www.linkedin.com/company/example-person") == ""
    assert canonical_linkedin_profile_url("https://www.linkedin.com/jobs/view/123") == ""
    assert normalize_navigation_type("Selector-based") == "Selector-based"
    assert navigation_type_key("Direct Url") == "direct_url"
    assert navigation_type_key("selector based") == "selector_based"
    duplicate_batch_sizes = distribute_slots_to_batches(4, [1, 1, 2, 2, 3, 4, 5, 6], "7/6/2026")
    assert sum(duplicate_batch_sizes.values()) == 4
    assert all(size >= 0 for size in duplicate_batch_sizes.values())
    assert all(batch in duplicate_batch_sizes for batch in [1, 2, 3, 4, 5, 6])
    ranked = rank_row_for_final(row, {"P1": "Not active", "P2": "Very active"})
    assert ranked["P1 Name"] == "Two"
    assert ranked["P1 Activity"] == "Very active"
    assert ranked["Category"] == "Hyper"
    planned_keys = {target["key"] for target in targets}
    staged_state = {
        "targets": {
            targets[0]["key"]: {"activity_value": "Not active"},
            targets[1]["key"]: {"activity_value": "Very active"},
        }
    }
    staged_rows = ready_final_rows(
        source_rows=[row],
        state=staged_state,
        all_target_keys=planned_keys,
        planned_target_keys=planned_keys,
    )
    assert len(staged_rows) == 1
    assert staged_rows[0]["Category"] == "Hyper"
    assert final_source_row_count([row], planned_keys, planned_keys) == 1
    assert not ready_final_rows(
        source_rows=[row],
        state={"targets": {targets[0]["key"]: {"activity_value": "Not active"}}},
        all_target_keys=planned_keys,
        planned_target_keys=planned_keys,
    )
    blank = rank_row_for_final(row, {"P1": "", "P2": ""})
    assert blank["Category"] == ""
    ambiguous_empty_success = {
        "error": False,
        "tabs": {
            "posts": {"total_visible": 0, "activities": []},
            "reactions": {"total_visible": 0, "activities": []},
            "comments": {"total_visible": 0, "activities": []},
        },
    }
    assert (
        activity_detail_empty_success_reason(ambiguous_empty_success)
        == "activity_feed_not_hydrated"
    )
    invalid_empty_success = {**ambiguous_empty_success, "page_url": "https://www.linkedin.com/404/"}
    assert activity_detail_empty_success_reason(invalid_empty_success) == "invalid_profile_or_404"
    explicit_empty_success = {
        "error": False,
        "tabs": {
            "posts": {
                "total_visible": 0,
                "activities": [],
                "feed_state": {"reason": "explicit_empty_state"},
            },
            "reactions": {
                "total_visible": 0,
                "activities": [],
                "feed_state": {"reason": "explicit_empty_state"},
            },
            "comments": {
                "total_visible": 0,
                "activities": [],
                "feed_state": {"reason": "explicit_empty_state"},
            },
        },
    }
    assert activity_detail_empty_success_reason(explicit_empty_success) == ""
    assert activity_level(explicit_empty_success) == "Not active"
    reader = LiveActivityReader(timeout=1, retries=1)
    reader.connected = True

    class FakeSession:
        cdp = None

        def __init__(self) -> None:
            self.calls = 0

        def read_activity_detail(
            self, _profile_url: str, max_seconds: float, navigation_type: str
        ) -> dict[str, Any]:
            self.calls += 1
            return {
                "error": True,
                "danger": "invalid_profile_or_404",
                "reason": "invalid_profile_or_404",
            }

        def _check_danger(self) -> str:
            return ""

    fake_session = FakeSession()
    reader.session = fake_session
    retained_error = reader(
        "https://www.linkedin.com/in/missing", {"navigation_type": "Direct Url"}
    )
    assert retained_error.get("reason") == "invalid_profile_or_404"
    assert len(retained_error.get("_attempts", [])) == 2


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Pre-final P1/P2 activity, then bridge Final and Prospects in stages."
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument(
        "--sheet-url", default=os.environ.get("LEAD_RESEARCH_SHEET_URL", DEFAULT_LEADS_SHEET_URL)
    )
    parser.add_argument(
        "--prefinal-tab", default=os.environ.get("LEAD_RESEARCH_PREFINAL_TAB", DEFAULT_PREFINAL_TAB)
    )
    parser.add_argument(
        "--final-tab", default=os.environ.get("LEAD_RESEARCH_FINAL_TAB", DEFAULT_FINAL_TAB)
    )
    parser.add_argument("--obf-url", default=os.environ.get("OBF_SHEET_URL", OBF_SHEET_URL))
    parser.add_argument("--prospects-tab", default=os.environ.get("OBF_PROSPECTS_TAB", "Prospects"))
    parser.add_argument(
        "--activity-sequence-tab",
        default=os.environ.get("ACTIVITY_SEQUENCE_TAB", DEFAULT_ACTIVITY_SEQUENCE_TAB),
    )
    parser.add_argument(
        "--credentials", default=os.environ.get("GOOGLE_SHEETS_CREDENTIALS", str(DEFAULT_CREDS))
    )
    parser.add_argument("--activity-timeout", type=float, default=180.0)
    parser.add_argument("--activity-retries", type=int, default=1)
    parser.add_argument("--max-consecutive-hard-failures", type=int, default=3)
    parser.add_argument(
        "--max-target-attempts",
        type=int,
        default=DEFAULT_MAX_TARGET_ATTEMPTS,
        help="Terminally skip one profile after this many unresolved Activity Check attempts (default: 4).",
    )
    parser.add_argument("--activity-fixture", default="")
    parser.add_argument(
        "--test-synthetic-activity",
        action="store_true",
        help="Test only: generate deterministic synthetic activity evidence; requires test destinations.",
    )
    parser.add_argument(
        "--queue-fingerprint",
        default="",
        help="Run one explicit queued batch instead of the oldest open batch.",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--live-dry-run",
        action="store_true",
        help="In dry-run mode, still read LinkedIn live activity.",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Prepare Activity Sequence/local session state, then exit before LinkedIn reads or Final writes.",
    )
    parser.add_argument(
        "--activity-only",
        action="store_true",
        help="Run only from an existing prepared local session; do not regenerate Activity Sequence.",
    )
    parser.add_argument(
        "--worker-id", default="", help="Run only targets assigned to one prepared activity worker."
    )
    parser.add_argument(
        "--retry-pending-404",
        action="store_true",
        help="Fresh-session retry of only targets deferred after an initial 404-like result.",
    )
    parser.add_argument(
        "--no-bridges",
        action="store_true",
        help="Record this lane only; defer Final/Prospects bridges to a later finalizer.",
    )
    parser.add_argument(
        "--finalize-only",
        action="store_true",
        help="Bridge the prepared session without opening LinkedIn or running activity reads.",
    )
    parser.add_argument(
        "--test-stop-cdp-after-completed",
        type=int,
        default=0,
        help="E2E test only: cleanly close this worker's Chrome after N completed targets.",
    )
    parser.add_argument(
        "--enqueue-current-prefinal",
        action="store_true",
        help="One-time bootstrap: snapshot the currently visible Pre-final rows into the local queue before activity work.",
    )
    parser.add_argument(
        "--final-bridge-threshold",
        type=float,
        default=0.90,
        help="Minimum resolved activity share required before the staged Final bridge (default: 0.90).",
    )
    parser.add_argument(
        "--prospects-bridge-delay-sec",
        type=int,
        default=300,
        help="Delay after a successful Final bridge before Final -> Prospects (default: 300).",
    )
    parser.add_argument(
        "--no-prospects-bridge", action="store_true", help="Stop after the staged Final bridge."
    )
    parser.add_argument("--delay-min-sec", type=int, default=5)
    parser.add_argument("--delay-max-sec", type=int, default=20)
    parser.add_argument("--batch-gap-min-sec", type=int, default=10)
    parser.add_argument("--batch-gap-max-sec", type=int, default=45)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.final_bridge_threshold <= 1:
        parser.error("--final-bridge-threshold must be between 0 and 1.")
    if args.prospects_bridge_delay_sec < 0:
        parser.error("--prospects-bridge-delay-sec must be >= 0.")
    if args.max_target_attempts < 1:
        parser.error("--max-target-attempts must be >= 1.")
    if args.worker_id and not args.activity_only:
        parser.error(
            "--worker-id requires --activity-only so the prepared assignment cannot change."
        )
    if args.finalize_only and not args.activity_only:
        parser.error("--finalize-only requires --activity-only.")
    if args.finalize_only and args.worker_id:
        parser.error("--finalize-only cannot target a single worker.")
    if args.finalize_only and args.no_bridges:
        parser.error("--finalize-only cannot be combined with --no-bridges.")
    if args.retry_pending_404 and (not args.activity_only or args.finalize_only):
        parser.error(
            "--retry-pending-404 requires --activity-only and cannot be used with --finalize-only."
        )
    if args.test_stop_cdp_after_completed < 0:
        parser.error("--test-stop-cdp-after-completed must be >= 0.")
    if args.test_stop_cdp_after_completed and not args.worker_id:
        parser.error("--test-stop-cdp-after-completed requires --worker-id.")
    if args.test_synthetic_activity and (
        not args.final_tab.endswith(" - Test") or not args.prospects_tab.endswith(" - Test")
    ):
        parser.error(
            "--test-synthetic-activity requires --final-tab and --prospects-tab ending in ' - Test'."
        )
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        run_self_tests()
        print("check_prefinal_activity self-tests passed")
        return 0
    if not Path(args.credentials).exists():
        raise SystemExit(f"Credentials file not found: {args.credentials}")
    result = run(args)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1
