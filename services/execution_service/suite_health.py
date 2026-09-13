"""Suite health and quarantine advice.

A test suite decays. Selectors drift, timing assumptions rot, and a handful of
intermittent tests quietly train everyone to re-run CI until it goes green —
at which point the suite has stopped being evidence of anything.

This module scores every test the platform has executed and says what to do
about it. It is a pure read over the flakiness ledger, so it costs no model
call and can run on every build.

**The distinction that matters.** A test that fails *every* time is not flaky:
it is a test that found something, or a test that is genuinely broken. Either
way, quarantining it hides the signal. Only a test with a mixed record — it
passes sometimes and fails sometimes with no code change in between — is a
quarantine candidate. Everything here is built around not confusing the two,
because the failure mode of getting it wrong is silently suppressing a real
defect, and nobody would ever notice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select

from services.observability.db import session_scope
from services.observability.models import FlakyTestRow

#: Below this many runs there is no evidence either way, and a verdict would be
#: noise. Two data points cannot distinguish flaky from broken.
MIN_RUNS_FOR_VERDICT = 4

#: A test failing at least this often, but not always, is unreliable.
FLAKE_RATE_UNRELIABLE = 0.10
#: Above this it is costing more than it proves.
FLAKE_RATE_QUARANTINE = 0.30
#: At or above this failure rate with no passes, it is broken, not flaky.
BROKEN_FAILURE_RATE = 0.95

VERDICTS = ("healthy", "unreliable", "quarantine_candidate", "broken", "unproven")


@dataclass
class TestHealth:
    test_id: str
    test_name: str = ""
    file_path: str = ""
    runs: int = 0
    failures: int = 0
    flakes: int = 0
    heals: int = 0
    quarantined: bool = False
    verdict: str = "unproven"
    reason: str = ""
    recommended_action: str = ""

    @property
    def instability(self) -> float:
        """How often this test did not simply pass."""
        return round((self.failures + self.flakes) / self.runs, 3) if self.runs else 0.0

    @property
    def flake_rate(self) -> float:
        return round(self.flakes / self.runs, 3) if self.runs else 0.0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["instability"] = self.instability
        payload["flake_rate"] = self.flake_rate
        return payload


@dataclass
class SuiteHealthReport:
    project_id: str = ""
    tests: list[TestHealth] = field(default_factory=list)

    def of_verdict(self, verdict: str) -> list[TestHealth]:
        return [test for test in self.tests if test.verdict == verdict]

    @property
    def quarantine_candidates(self) -> list[TestHealth]:
        return [t for t in self.of_verdict("quarantine_candidate") if not t.quarantined]

    @property
    def health_score(self) -> float:
        """Share of tests with a verdict that are healthy, 0-100."""
        judged = [t for t in self.tests if t.verdict != "unproven"]
        if not judged:
            return 0.0
        healthy = sum(1 for t in judged if t.verdict == "healthy")
        return round(100.0 * healthy / len(judged), 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "tests_tracked": len(self.tests),
            "health_score": self.health_score,
            "by_verdict": {v: len(self.of_verdict(v)) for v in VERDICTS},
            "quarantined": sum(1 for t in self.tests if t.quarantined),
            "quarantine_candidates": [t.to_dict() for t in self.quarantine_candidates],
            "tests": [t.to_dict() for t in self.tests],
            "summary": self.summary(),
        }

    def summary(self) -> str:
        if not self.tests:
            return "No test has been executed yet, so there is nothing to judge."
        unproven = len(self.of_verdict("unproven"))
        parts = [
            f"{len(self.tests)} test(s) tracked, health score {self.health_score}%",
            f"{len(self.of_verdict('unreliable'))} unreliable",
            f"{len(self.quarantine_candidates)} quarantine candidate(s)",
            f"{len(self.of_verdict('broken'))} consistently failing",
        ]
        if unproven:
            parts.append(f"{unproven} with too few runs to judge")
        return "; ".join(parts) + "."


# --------------------------------------------------------------------------- #
def _verdict(row: FlakyTestRow) -> tuple[str, str, str]:
    """Classify one test: (verdict, reason, recommended action)."""
    runs, failures, flakes = row.runs, row.failures, row.flakes
    if runs < MIN_RUNS_FOR_VERDICT:
        return (
            "unproven",
            f"only {runs} run(s) — too few to tell a flaky test from a broken one",
            "run the suite a few more times before acting",
        )

    failure_rate = failures / runs
    instability = (failures + flakes) / runs

    if failure_rate >= BROKEN_FAILURE_RATE:
        # Never a quarantine candidate. A test that has never passed is either
        # reporting a real defect or is itself wrong; hiding it loses that.
        return (
            "broken",
            f"failed {failures} of {runs} run(s) and effectively never passes",
            "investigate — this is a defect or a wrong test, NOT flakiness; do not quarantine",
        )

    if instability >= FLAKE_RATE_QUARANTINE:
        return (
            "quarantine_candidate",
            f"unstable in {round(100 * instability)}% of {runs} run(s) while still passing sometimes",
            "quarantine and fix: at this rate it costs more attention than it provides",
        )

    if instability >= FLAKE_RATE_UNRELIABLE:
        return (
            "unreliable",
            f"unstable in {round(100 * instability)}% of {runs} run(s)",
            "watch it; stabilise the wait or selector before it gets worse",
        )

    if row.heals > 0:
        return (
            "healthy",
            f"stable across {runs} run(s), self-healed {row.heals} time(s)",
            "none — but repeated healing suggests the application markup is churning",
        )
    return "healthy", f"passed consistently across {runs} run(s)", "none"


def suite_health(project_id: str, *, limit: int = 500) -> SuiteHealthReport:
    """Score every test this project has executed."""
    report = SuiteHealthReport(project_id=project_id)
    with session_scope() as session:
        rows = list(
            session.execute(
                select(FlakyTestRow)
                .where(FlakyTestRow.project_id == project_id)
                .order_by(FlakyTestRow.flakes.desc(), FlakyTestRow.failures.desc())
                .limit(limit)
            ).scalars()
        )
        for row in rows:
            verdict, reason, action = _verdict(row)
            report.tests.append(
                TestHealth(
                    test_id=row.test_id,
                    test_name=row.test_name,
                    file_path=row.file_path,
                    runs=row.runs,
                    failures=row.failures,
                    flakes=row.flakes,
                    heals=row.heals,
                    quarantined=bool(row.quarantined),
                    verdict=verdict,
                    reason=reason,
                    recommended_action=action,
                )
            )

    order = {v: i for i, v in enumerate(("broken", "quarantine_candidate", "unreliable", "healthy", "unproven"))}
    report.tests.sort(key=lambda t: (order.get(t.verdict, 9), -t.instability))
    return report


def set_quarantine(project_id: str, test_id: str, quarantined: bool, *, force: bool = False) -> dict[str, Any]:
    """Quarantine or release one test.

    Refuses to quarantine a consistently failing test unless forced. That test
    is not flaky — it is the suite doing its job — and suppressing it is how a
    real defect reaches production with a green pipeline.
    """
    with session_scope() as session:
        row = session.execute(
            select(FlakyTestRow).where(
                FlakyTestRow.project_id == project_id, FlakyTestRow.test_id == test_id
            )
        ).scalar_one_or_none()
        if row is None:
            return {"ok": False, "reason": f"no execution history for {test_id}"}

        if quarantined and not force:
            verdict, reason, _action = _verdict(row)
            if verdict == "broken":
                return {
                    "ok": False,
                    "verdict": verdict,
                    "reason": (
                        f"{test_id} {reason}. That is a finding, not flakiness — "
                        "quarantining it would hide a real failure. Pass force=True only "
                        "if you have confirmed the test itself is wrong."
                    ),
                }
            if verdict == "unproven":
                return {"ok": False, "verdict": verdict, "reason": reason}

        row.quarantined = quarantined
        return {"ok": True, "test_id": test_id, "quarantined": quarantined}


def auto_quarantine(project_id: str, *, apply: bool = False, max_tests: int = 10) -> dict[str, Any]:
    """Recommend — and optionally apply — quarantine for unstable tests.

    Defaults to recommending only. Silently disabling tests is exactly the
    behaviour that makes a suite untrustworthy, so applying is opt-in and
    always reports precisely what it did.
    """
    report = suite_health(project_id)
    candidates = report.quarantine_candidates[:max_tests]
    applied: list[str] = []
    if apply:
        for test in candidates:
            result = set_quarantine(project_id, test.test_id, True)
            if result.get("ok"):
                applied.append(test.test_id)
                test.quarantined = True

    return {
        "applied": apply,
        "quarantined": applied,
        "candidates": [t.to_dict() for t in candidates],
        "skipped_broken": [t.test_id for t in report.of_verdict("broken")],
        "summary": (
            f"{len(applied)} test(s) quarantined"
            if apply
            else f"{len(candidates)} test(s) recommended for quarantine (nothing changed)"
        ),
    }


__all__ = [
    "MIN_RUNS_FOR_VERDICT",
    "SuiteHealthReport",
    "TestHealth",
    "VERDICTS",
    "auto_quarantine",
    "set_quarantine",
    "suite_health",
]
