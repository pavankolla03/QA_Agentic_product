"""Suite health and quarantine.

Quarantine is the one feature in this platform that can make a suite *worse*.
Disabling a test that is reporting a real defect turns a red pipeline green and
nobody finds out until production. So most of these tests are about the refusal
path: what the platform declines to quarantine, and why.
"""

from __future__ import annotations

import pytest

from services.execution_service.suite_health import (
    MIN_RUNS_FOR_VERDICT,
    auto_quarantine,
    set_quarantine,
    suite_health,
)
from services.observability.db import session_scope
from services.observability.models import FlakyTestRow

PROJECT = "prj_health"


def _record(test_id: str, runs: int, failures: int = 0, flakes: int = 0, heals: int = 0) -> None:
    with session_scope() as session:
        session.add(
            FlakyTestRow(
                project_id=PROJECT,
                test_id=test_id,
                test_name=f"scenario {test_id}",
                file_path=f"tests/{test_id}.spec.ts",
                runs=runs,
                failures=failures,
                flakes=flakes,
                heals=heals,
            )
        )


def _verdict_of(test_id: str) -> str:
    return next(t.verdict for t in suite_health(PROJECT).tests if t.test_id == test_id)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def test_a_consistently_passing_test_is_healthy() -> None:
    _record("TC-OK", runs=20)
    assert _verdict_of("TC-OK") == "healthy"


def test_a_test_that_never_passes_is_broken_not_flaky() -> None:
    """The distinction the whole feature rests on."""
    _record("TC-RED", runs=10, failures=10)
    assert _verdict_of("TC-RED") == "broken"


def test_an_intermittent_test_is_a_quarantine_candidate() -> None:
    _record("TC-FLAKE", runs=10, flakes=5)
    assert _verdict_of("TC-FLAKE") == "quarantine_candidate"


def test_a_slightly_unstable_test_is_only_watched() -> None:
    _record("TC-WOBBLE", runs=20, flakes=3)
    assert _verdict_of("TC-WOBBLE") == "unreliable"


def test_too_few_runs_yields_no_verdict() -> None:
    """Two data points cannot tell flaky from broken."""
    _record("TC-NEW", runs=MIN_RUNS_FOR_VERDICT - 1, failures=1)
    health = next(t for t in suite_health(PROJECT).tests if t.test_id == "TC-NEW")
    assert health.verdict == "unproven"
    assert "too few" in health.reason


def test_repeated_healing_is_surfaced_even_when_healthy() -> None:
    _record("TC-HEALED", runs=12, heals=4)
    health = next(t for t in suite_health(PROJECT).tests if t.test_id == "TC-HEALED")
    assert health.verdict == "healthy"
    assert "churn" in health.recommended_action


# --------------------------------------------------------------------------- #
# Refusing to hide a real failure
# --------------------------------------------------------------------------- #
def test_quarantining_a_broken_test_is_refused() -> None:
    _record("TC-RED", runs=10, failures=10)
    result = set_quarantine(PROJECT, "TC-RED", True)

    assert result["ok"] is False
    assert "hide a real failure" in result["reason"]
    with session_scope() as session:
        row = session.query(FlakyTestRow).filter_by(test_id="TC-RED").one()
        assert row.quarantined is False


def test_a_broken_test_can_still_be_quarantined_deliberately() -> None:
    """The refusal is a guard rail, not a lock: a human may overrule it."""
    _record("TC-RED", runs=10, failures=10)
    assert set_quarantine(PROJECT, "TC-RED", True, force=True)["ok"] is True


def test_quarantining_an_unproven_test_is_refused() -> None:
    _record("TC-NEW", runs=2, failures=1)
    assert set_quarantine(PROJECT, "TC-NEW", True)["ok"] is False


def test_an_unknown_test_cannot_be_quarantined() -> None:
    assert set_quarantine(PROJECT, "TC-GHOST", True)["ok"] is False


def test_releasing_a_test_is_never_refused() -> None:
    """Re-enabling a test can only increase what the suite reports."""
    _record("TC-RED", runs=10, failures=10)
    set_quarantine(PROJECT, "TC-RED", True, force=True)
    assert set_quarantine(PROJECT, "TC-RED", False)["ok"] is True


# --------------------------------------------------------------------------- #
# Bulk behaviour
# --------------------------------------------------------------------------- #
def test_auto_quarantine_recommends_without_changing_anything() -> None:
    _record("TC-FLAKE", runs=10, flakes=5)
    result = auto_quarantine(PROJECT)

    assert result["applied"] is False
    assert result["quarantined"] == []
    assert [c["test_id"] for c in result["candidates"]] == ["TC-FLAKE"]
    with session_scope() as session:
        assert session.query(FlakyTestRow).filter_by(test_id="TC-FLAKE").one().quarantined is False


def test_auto_quarantine_applies_only_when_asked_and_never_to_broken_tests() -> None:
    _record("TC-FLAKE", runs=10, flakes=5)
    _record("TC-RED", runs=10, failures=10)

    result = auto_quarantine(PROJECT, apply=True)
    assert result["quarantined"] == ["TC-FLAKE"]
    assert result["skipped_broken"] == ["TC-RED"]

    with session_scope() as session:
        assert session.query(FlakyTestRow).filter_by(test_id="TC-FLAKE").one().quarantined is True
        assert session.query(FlakyTestRow).filter_by(test_id="TC-RED").one().quarantined is False


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def test_worst_tests_are_listed_first() -> None:
    _record("TC-OK", runs=20)
    _record("TC-FLAKE", runs=10, flakes=5)
    _record("TC-RED", runs=10, failures=10)
    verdicts = [t.verdict for t in suite_health(PROJECT).tests]
    assert verdicts[0] == "broken"
    assert verdicts.index("quarantine_candidate") < verdicts.index("healthy")


def test_the_health_score_ignores_tests_with_no_verdict() -> None:
    _record("TC-OK", runs=20)
    _record("TC-NEW", runs=1)
    assert suite_health(PROJECT).health_score == 100.0


def test_an_empty_project_says_so_rather_than_scoring_zero_health() -> None:
    report = suite_health("prj_nothing_here")
    assert report.tests == []
    assert "nothing to judge" in report.summary()


def test_the_report_serialises_for_the_api() -> None:
    _record("TC-FLAKE", runs=10, flakes=5)
    payload = suite_health(PROJECT).to_dict()
    assert payload["by_verdict"]["quarantine_candidate"] == 1
    assert payload["tests"][0]["instability"] == 0.5
    assert payload["quarantine_candidates"]


@pytest.mark.parametrize("verdict", ["broken", "quarantine_candidate", "unreliable", "unproven"])
def test_every_non_healthy_verdict_explains_itself(verdict: str) -> None:
    _record("TC-RED", runs=10, failures=10)
    _record("TC-FLAKE", runs=10, flakes=5)
    _record("TC-WOBBLE", runs=20, flakes=3)
    _record("TC-NEW", runs=1)

    tests = [t for t in suite_health(PROJECT).tests if t.verdict == verdict]
    assert tests, f"no test classified as {verdict}"
    for test in tests:
        assert test.reason.strip()
        assert test.recommended_action.strip()
