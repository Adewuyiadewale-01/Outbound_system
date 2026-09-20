import pytest

from scripts.post_engagement import (
    ACTIVITY_ASSESSMENT_VERSION,
    DEFAULT_CONFIG,
    PROFILE_PARSER_VERSION,
    assess_engaged_candidate,
    choose_like_target,
    classify_location,
    current_engagement_batch,
    ensure_engagement_batches,
    ensure_obf_diversions,
    final_action_queue,
    parse_follower_count,
    parse_relative_age_hours,
    qualifies,
    recommendation_for,
    validate_profile_gate,
)


def test_relative_time_and_follower_parsing():
    assert parse_relative_age_hours("1d •") == 24
    assert parse_relative_age_hours("3h •") == 3
    assert parse_relative_age_hours("1w •") == 168
    assert parse_follower_count("35,345 followers") == 35345
    assert parse_follower_count("5K followers") == 5000


def test_geography_tiers_are_data_driven():
    assert classify_location("New York, United States")["geography_tier"] == 1
    assert classify_location("Amsterdam, Netherlands")["country_code"] == "NL"
    assert classify_location("Lagos, Nigeria")["geography_tier"] == 3
    assert classify_location("Somewhere remote")["geography_tier"] == 4


def test_like_assignment_is_stable_and_bounded():
    first = choose_like_target("2026-09-06", "https://www.linkedin.com/in/example/", DEFAULT_CONFIG)
    second = choose_like_target(
        "2026-09-06", "https://www.linkedin.com/in/example/", DEFAULT_CONFIG
    )
    assert first == second
    assert 1 <= first <= 3


def test_engagement_batches_are_balanced_and_persisted():
    campaign = {"day": "2026-09-06", "target": 49, "engaged": 0}
    batches = ensure_engagement_batches(campaign, DEFAULT_CONFIG)
    assert sorted(batch["target"] for batch in batches) == [16, 16, 17]
    assert sum(batch["target"] for batch in batches) == 49
    assert ensure_engagement_batches(campaign, DEFAULT_CONFIG) == batches
    assert current_engagement_batch(campaign, DEFAULT_CONFIG)["number"] == 1


def test_obf_diversion_plan_is_stable_per_candidate():
    campaign = {
        "day": "2026-09-06",
        "candidates": [
            {"profile_url": "https://www.linkedin.com/in/one/"},
            {"profile_url": "https://www.linkedin.com/in/two/"},
        ],
    }
    ensure_obf_diversions(campaign, DEFAULT_CONFIG)
    initial = [(c["obf_diversion"], c["obf_diversion_seconds"]) for c in campaign["candidates"]]
    ensure_obf_diversions(campaign, DEFAULT_CONFIG)
    assert [
        (c["obf_diversion"], c["obf_diversion_seconds"]) for c in campaign["candidates"]
    ] == initial


def test_any_activity_signal_qualifies():
    assert qualifies({"reactions": 5, "comments": 0, "posts": 0}, DEFAULT_CONFIG)
    assert qualifies({"reactions": 0, "comments": 3, "posts": 0}, DEFAULT_CONFIG)
    assert qualifies({"reactions": 0, "comments": 0, "posts": 3}, DEFAULT_CONFIG)
    assert not qualifies({"reactions": 4, "comments": 2, "posts": 2}, DEFAULT_CONFIG)


def test_profile_gate_rejects_global_ui_and_contact_labels():
    with pytest.raises(RuntimeError, match="invalid_name"):
        validate_profile_gate(
            {
                "name": "0 notifications",
                "location": "New York, United States",
                "follower_text": "100 followers",
                "follower_source": "header",
            }
        )
    with pytest.raises(RuntimeError, match="invalid_location"):
        validate_profile_gate(
            {
                "name": "Ada Lovelace",
                "location": "Contact info",
                "follower_text": "100 followers",
                "follower_source": "header",
            }
        )


def test_profile_gate_accepts_valid_profile_metadata():
    validate_profile_gate(
        {
            "name": "Ada Lovelace",
            "location": "New York, United States",
            "follower_text": "35,345 followers",
            "follower_source": "header",
        }
    )
    validate_profile_gate(
        {
            "name": "Ada Lovelace",
            "location": "New York, United States",
            "follower_text": "35,345 followers",
            "follower_source": "activity",
        }
    )


def test_profile_gate_rejects_unbounded_follower_source():
    with pytest.raises(RuntimeError, match="unbounded_follower_source"):
        validate_profile_gate(
            {
                "name": "Ada Lovelace",
                "location": "New York, United States",
                "follower_text": "35,345 followers",
                "follower_source": "missing",
            }
        )


class FakeActivitySession:
    def __init__(self):
        self.calls = 0

    def read_activity_detail(self, *_args, **_kwargs):
        self.calls += 1
        return {
            "tabs": {
                "reactions": {"activities": [{"time_text": "1d"}] * 5},
                "comments": {"activities": []},
                "posts": {"activities": []},
            }
        }


def test_activity_assessment_is_checkpointed_and_recommends_once():
    session = FakeActivitySession()
    candidate = {"profile_url": "https://www.linkedin.com/in/example/", "follower_count": 100}
    assert assess_engaged_candidate(session, candidate, DEFAULT_CONFIG)
    assert candidate["recommendation"]["action"] == "connect"
    assert candidate["recommendation"]["assessment_version"] == ACTIVITY_ASSESSMENT_VERSION
    assert assess_engaged_candidate(session, candidate, DEFAULT_CONFIG)
    assert session.calls == 1


def test_active_high_follower_profile_is_recommended_for_follow():
    candidate = {"activity_counts": {"reactions": 5}, "follower_count": 5001}
    assert recommendation_for(candidate, DEFAULT_CONFIG)["action"] == "follow"


def test_final_action_queue_contains_only_complete_saved_recommendations():
    complete = {
        "profile_url": "https://www.linkedin.com/in/complete/",
        "geography_tier": 1,
        "activity_counts": {"reactions": 5, "comments": 0, "posts": 0},
        "profile_parser_version": PROFILE_PARSER_VERSION,
        "activity_assessment_status": "complete",
        "recommendation": {"action": "connect", "assessment_version": ACTIVITY_ASSESSMENT_VERSION},
    }
    incomplete = {
        "profile_url": "https://www.linkedin.com/in/incomplete/",
        "activity_assessment_status": "failed",
        "recommendation": {"action": "connect", "assessment_version": ACTIVITY_ASSESSMENT_VERSION},
    }
    queue = final_action_queue([incomplete, complete], {"day": "2026-09-06"})
    assert queue == [complete]


def test_final_action_queue_randomly_interleaves_without_three_action_streaks():
    candidates = []
    for action in ("connect", "follow"):
        for index in range(6):
            candidates.append(
                {
                    "profile_url": f"https://www.linkedin.com/in/{action}-{index}/",
                    "geography_tier": 1,
                    "activity_counts": {"reactions": 5, "comments": 0, "posts": 0},
                    "profile_parser_version": PROFILE_PARSER_VERSION,
                    "activity_assessment_status": "complete",
                    "recommendation": {
                        "action": action,
                        "assessment_version": ACTIVITY_ASSESSMENT_VERSION,
                    },
                }
            )
    queue = final_action_queue(candidates, {"day": "2026-09-06"})
    actions = [candidate["recommendation"]["action"] for candidate in queue]
    assert not any(actions[i] == actions[i + 1] == actions[i + 2] for i in range(len(actions) - 2))
