"""End-to-end: does the platform produce automation a QA engineer would accept?

These assertions are about the *artifacts*, not the plumbing. A run that
completes but emits untagged Gherkin, locators in step files, or a page object
that ignores the repository's base class has not done its job.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.aiqa_types.enums import RunMode, RunStatus
from packages.aiqa_types.models import RunRequest


@pytest.fixture
async def completed_run(engine, project, org_user):
    """One full auto-approved run, shared by the assertions below."""
    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(
            project_id=project.id,
            instruction="Automate the Resident Registration functionality",
            mode=RunMode.FULL,
            auto_approve=True,
        ),
        user_id=user_id,
        org_id=org_id,
    )
    result = await engine.run_to_completion(run_id, auto_approve=True)
    assert result.status == RunStatus.SUCCEEDED, result.error

    from services.observability.db import session_scope
    from services.observability.models import RunRow

    with session_scope() as session:
        row = session.get(RunRow, run_id)
        return {
            "run_id": run_id,
            "requirement": row.requirement,
            "test_plan": row.test_plan,
            "code_bundle": row.code_bundle,
            "standards_report": row.standards_report,
            "report": row.report,
            "metadata": row.metadata_json,
            "visited": (row.metadata_json or {}).get("visited", []),
        }


# =========================================================================== #
# Pipeline shape
# =========================================================================== #
async def test_the_whole_pipeline_runs(completed_run) -> None:
    visited = completed_run["visited"]
    for node in ("requirement", "repository", "test_design", "code_generation", "standards", "reporting"):
        assert node in visited, f"{node} never ran: {visited}"


# =========================================================================== #
# Requirement quality
# =========================================================================== #
async def test_requirement_is_structured_and_testable(completed_run) -> None:
    requirement = completed_run["requirement"]
    assert requirement is not None
    assert "Resident" in requirement["title"], requirement["title"]
    criteria = requirement["acceptance_criteria"]
    assert len(criteria) >= 3
    assert all(c["text"].strip() for c in criteria)
    assert 0.0 <= requirement["ambiguity_score"] <= 1.0


# =========================================================================== #
# Test-plan quality
# =========================================================================== #
async def test_plan_covers_happy_path_and_negatives(completed_run) -> None:
    scenarios = [s for f in completed_run["test_plan"]["features"] for s in f["scenarios"]]
    assert len(scenarios) >= 3
    assert any(s["negative"] for s in scenarios), "no negative scenario was designed"
    assert any("@smoke" in s["tags"] for s in scenarios), "the critical path is not marked @smoke"


async def test_every_scenario_is_tagged_and_uniquely_identified(completed_run) -> None:
    scenarios = [s for f in completed_run["test_plan"]["features"] for s in f["scenarios"]]
    ids = [s["test_id"] for s in scenarios]
    assert all(ids), "every scenario needs a test id"
    assert len(set(ids)) == len(ids), f"duplicate test ids: {ids}"
    assert all(i.startswith("TC-") for i in ids), ids
    assert all(s["tags"] for s in scenarios), "every scenario must carry at least one tag"


async def test_every_scenario_asserts_something(completed_run) -> None:
    for feature in completed_run["test_plan"]["features"]:
        for scenario in feature["scenarios"]:
            keywords = [step["keyword"] for step in scenario["steps"]]
            assert "Then" in keywords, f"{scenario['test_id']} never asserts anything"


# =========================================================================== #
# Generated code quality
# =========================================================================== #
async def test_files_land_in_the_repository_layout(completed_run, repo_copy: Path) -> None:
    changes = completed_run["code_bundle"]["changes"]
    assert changes

    by_kind: dict[str, list[str]] = {}
    for change in changes:
        by_kind.setdefault(change["kind"], []).append(change["path"])
    assert "feature" in by_kind, "no feature file was generated"

    for path in by_kind.get("feature", []):
        assert path.startswith("tests/features/"), path
    for path in by_kind.get("page_object", []):
        assert path.startswith("tests/pages/"), path
    for path in by_kind.get("step_definition", []):
        assert path.startswith("tests/steps/"), path

    for change in changes:
        assert (repo_copy / change["path"]).exists(), f"{change['path']} was never written"


async def test_generated_gherkin_is_valid_and_tagged(completed_run, repo_copy: Path) -> None:
    feature_files = [
        repo_copy / c["path"] for c in completed_run["code_bundle"]["changes"] if c["kind"] == "feature"
    ]
    assert feature_files

    for path in feature_files:
        text = path.read_text(encoding="utf-8")
        assert "Feature:" in text
        assert "Scenario" in text
        assert "@" in text, "generated feature carries no tags"
        # Every Scenario line must be preceded by a tag line.
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if line.strip().startswith(("Scenario:", "Scenario Outline:")):
                probe = index - 1
                while probe >= 0 and not lines[probe].strip():
                    probe -= 1
                assert probe >= 0 and lines[probe].strip().startswith("@"), (
                    f"untagged scenario at line {index + 1} of {path.name}"
                )


async def test_generated_code_obeys_the_standards(completed_run) -> None:
    report = completed_run["standards_report"]
    assert report is not None
    errors = [v for v in report["violations"] if v["severity"] in ("error", "critical")]
    assert not errors, f"generated code violates the org standard: {errors}"


async def test_no_hard_waits_or_credentials_in_generated_code(completed_run, repo_copy: Path) -> None:
    import re

    for change in completed_run["code_bundle"]["changes"]:
        text = (repo_copy / change["path"]).read_text(encoding="utf-8")
        assert "waitForTimeout" not in text, f"hard wait in {change['path']}"
        assert not re.search(r"(?i)password\s*[:=]\s*['\"][^'\"]{4,}['\"]", text), (
            f"credential literal in {change['path']}"
        )
        assert ".only(" not in text, f"focused test left in {change['path']}"


async def test_step_definitions_do_not_contain_raw_locators(completed_run, repo_copy: Path) -> None:
    """POM discipline: selectors live in page objects, steps call methods."""
    step_files = [
        repo_copy / c["path"]
        for c in completed_run["code_bundle"]["changes"]
        if c["kind"] == "step_definition"
    ]
    for path in step_files:
        text = path.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))
        assert "page.locator(" not in code, f"raw locator in {path.name}"
        assert "page.getBy" not in code, f"raw locator in {path.name}"


async def test_existing_assets_are_reused_not_duplicated(completed_run) -> None:
    """The repository already has LoginPage/BasePage — they must not be regenerated."""
    paths = {c["path"] for c in completed_run["code_bundle"]["changes"]}
    assert "tests/pages/BasePage.ts" not in paths
    assert "tests/pages/LoginPage.ts" not in paths
    assert "tests/fixtures/test-fixtures.ts" not in paths


async def test_unverified_locators_are_marked_not_faked(completed_run, repo_copy: Path) -> None:
    """With no reachable application, generated locators must be flagged for review."""
    page_objects = [
        repo_copy / c["path"]
        for c in completed_run["code_bundle"]["changes"]
        if c["kind"] == "page_object"
    ]
    if not page_objects:
        pytest.skip("this run reused existing page objects")
    combined = "\n".join(p.read_text(encoding="utf-8") for p in page_objects)
    assert "TODO(aiqa)" in combined, (
        "the app was unreachable, so unverified locators must carry a TODO marker "
        "rather than looking authoritative"
    )


# =========================================================================== #
# Reporting
# =========================================================================== #
async def test_report_is_complete_and_honest(completed_run) -> None:
    report = completed_run["report"]
    assert report is not None
    assert report["headline"]
    assert report["next_actions"], "a report with nothing to do next is not useful"
    assert report["scenarios_designed"] > 0

    markdown = report["markdown"]
    assert "# QAgentic run" in markdown
    assert "## Summary" in markdown
    assert "## Next actions" in markdown
    # The fixture has no Playwright and no crawl, so there are two honest
    # outcomes and the report must be one of them: it could not execute, or the
    # steps it generated have no verified element behind them. What it must
    # never do is lead with the count of files written, which reads as finished
    # work.
    headline = report["headline"].lower()
    assert (
        "execution blocked" in headline
        or "automation_blocked" in headline
        or report["tests_total"] > 0
    ), report["headline"]

    assert "<!doctype html>" in report["html"].lower()


async def test_report_artifacts_are_written_to_disk(completed_run) -> None:
    from sqlalchemy import select

    from services.observability.db import session_scope
    from services.observability.models import ArtifactRow

    with session_scope() as session:
        artifacts = list(
            session.execute(select(ArtifactRow).where(ArtifactRow.run_id == completed_run["run_id"])).scalars()
        )
    assert artifacts
    for artifact in artifacts:
        assert Path(artifact.path).exists()
        assert artifact.size_bytes > 0


# =========================================================================== #
# Governance
# =========================================================================== #
async def test_every_file_write_is_audited(completed_run) -> None:
    from sqlalchemy import select

    from services.observability.db import session_scope
    from services.observability.models import AuditRow

    with session_scope() as session:
        entries = list(
            session.execute(select(AuditRow).where(AuditRow.run_id == completed_run["run_id"])).scalars()
        )
    actions = {entry.action for entry in entries}
    assert "run_create" in actions
    assert "file_write" in actions, "writing to the workspace must always be audited"


async def test_no_secret_reaches_a_prompt(completed_run, repo_copy: Path) -> None:
    """The end-to-end guarantee: nothing secret-shaped is stored in a prompt preview."""
    from sqlalchemy import select

    from services.observability.db import session_scope
    from services.observability.models import LLMCallRow

    with session_scope() as session:
        calls = list(
            session.execute(select(LLMCallRow).where(LLMCallRow.run_id == completed_run["run_id"])).scalars()
        )
    assert calls
    from packages.security import contains_secret

    for call in calls:
        assert not contains_secret(call.prompt_preview), f"secret in the prompt for {call.agent}"
