"""Tests for the ported signal verification (``job_discovery/src/signals.mjs``)."""

from __future__ import annotations

from outbound.job_discovery.signals import verify_signals


def test_verifies_the_three_strong_signals() -> None:
    result = verify_signals(
        {
            "title": "Junior Python Developer",
            "description": "This fully remote team needs solid Python experience.",
            "role": "Python Developer",
        }
    )
    assert result["juniorStatus"] == "verified"
    assert result["remoteStatus"] == "verified"
    assert result["pythonStatus"] == "verified"
    assert result["score"] == 110
    assert result["reviewReason"] == ""
    assert "Junior" in result["evidenceText"]


def test_sends_conflicting_seniority_wording_to_review() -> None:
    result = verify_signals(
        {
            "title": "Junior / Senior Engineer",
            "description": "A role with conflicting level wording.",
            "role": "Software Engineer",
        }
    )
    assert result["juniorStatus"] == "conflicting"
    assert result["reviewReason"] == "Conflicting signals"


def test_records_unambiguous_hybrid_and_onsite_location_evidence_separately_from_remote() -> None:
    hybrid = verify_signals(
        {
            "title": "Python Developer",
            "description": "This role follows a hybrid work model.",
            "role": "Python Developer",
        }
    )
    onsite = verify_signals(
        {
            "title": "Python Developer",
            "description": "This is an on-site role in Lagos.",
            "role": "Python Developer",
        }
    )
    assert hybrid["remoteStatus"] == "hybrid_verified"
    assert onsite["remoteStatus"] == "onsite_verified"


def test_sends_mixed_remote_and_hybrid_job_page_evidence_to_review() -> None:
    result = verify_signals(
        {
            "title": "Senior Product Software Engineer (Python)",
            "location": "SF Bay Area, Chicago, LA or Remote",
            "description": (
                "Half of the company is remote. Flexible Work Environment: "
                "Hybrid work model, remote work options."
            ),
            "role": "Python Developer",
        }
    )
    assert result["remoteStatus"] == "conflicting"
    assert result["reviewReason"] == "Conflicting signals"
    assert result["juniorStatus"] == "senior_verified"


def test_plain_postings_report_not_found_with_zero_score() -> None:
    result = verify_signals(
        {
            "title": "Backend Developer",
            "description": "Build services.",
            "role": "Backend Developer",
        }
    )
    assert result["juniorStatus"] == "not_found"
    assert result["remoteStatus"] == "not_found"
    assert result["pythonStatus"] == "not_found"
    assert result["score"] == 0
    assert result["reviewReason"] == ""


def test_partial_title_for_python_role_goes_to_review() -> None:
    result = verify_signals(
        {"title": "Dev", "description": "Build things.", "role": "Python Developer"}
    )
    assert result["juniorStatus"] == "unsure"
    assert result["remoteStatus"] == "unsure"
    assert result["pythonStatus"] == "unsure"
    assert result["reviewReason"] == "Signal could not be confirmed"


def test_custom_rule_terms_override_defaults() -> None:
    rules = {
        "junior": ["mid-level"],
        "senior": ["senior"],
        "remote": ["remote"],
        "hybrid": ["hybrid"],
        "onsite": ["onsite"],
        "python": ["python"],
    }
    result = verify_signals(
        {
            "title": "Mid-Level Python Developer",
            "description": "Remote role.",
            "role": "Python Developer",
        },
        rules,
    )
    assert result["juniorStatus"] == "verified"
    assert result["remoteStatus"] == "verified"
    assert result["pythonStatus"] == "verified"


def test_level_one_term_maps_to_engineer_or_developer_i() -> None:
    result = verify_signals(
        {"title": "Software Engineer I", "description": "Great team.", "role": "Software Engineer"},
        {"junior": ["I"]},
    )
    assert result["juniorStatus"] == "verified"


def test_legacy_non_remote_terms_split_between_hybrid_and_onsite() -> None:
    legacy = {"nonRemote": ["hybrid", "in office"]}
    hybrid = verify_signals(
        {"title": "Python Developer", "description": "A hybrid role.", "role": "Python Developer"},
        legacy,
    )
    onsite = verify_signals(
        {
            "title": "Python Developer",
            "description": "Work in office daily.",
            "role": "Python Developer",
        },
        legacy,
    )
    assert hybrid["remoteStatus"] == "hybrid_verified"
    assert onsite["remoteStatus"] == "onsite_verified"


def test_seniority_from_description_is_not_conclusive() -> None:
    result = verify_signals(
        {
            "title": "Python Developer",
            "description": "You will report to a senior engineer.",
            "role": "Python Developer",
        }
    )
    assert result["juniorStatus"] == "not_found"
