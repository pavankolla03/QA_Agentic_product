"""End-to-end self-check.

Runs the whole platform against a disposable copy of the sample repository with
no LLM credentials and no application under test — the worst-case install. If
this passes, the wiring is sound: orchestration, approval gates, suspend/resume,
standards enforcement, tracing, cost accounting and reporting.

Usage
-----
    python -m scripts.selfcheck                 # offline (deterministic provider)
    python -m scripts.selfcheck --live          # use whatever providers are configured
    python -m scripts.selfcheck --keep          # keep the temporary workspace
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Windows consoles still default to cp1252; never let output encoding fail a check.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass


def _banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def _ok(label: str, value: object = "") -> None:
    print(f"  [ok]   {label}" + (f" — {value}" if value != "" else ""))


def _fail(label: str, value: object = "") -> None:
    print(f"  [FAIL] {label}" + (f" — {value}" if value != "" else ""))


async def main(live: bool = False, keep: bool = False) -> int:
    workspace = Path(tempfile.mkdtemp(prefix="aiqa-selfcheck-"))
    db_path = workspace / "selfcheck.db"
    os.environ["AIQA_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["AIQA_ARTIFACTS_DIR"] = str(workspace / "artifacts")
    os.environ.setdefault("AIQA_PER_RUN_COST_LIMIT_USD", "5.0")

    from configs.settings import reset_config_cache

    reset_config_cache()

    from packages.aiqa_types.enums import RunMode, RunStatus
    from packages.aiqa_types.models import RunRequest, new_id
    from services.agent_engine import AgentEngine
    from services.observability.db import init_db, reset_db_state, session_scope
    from services.observability.models import (
        AgentTraceRow,
        ApprovalRow,
        AuditRow,
        LLMCallRow,
        OrgRow,
        ProjectRow,
        RunRow,
        ToolCallRow,
        UserRow,
    )

    reset_db_state()
    init_db()

    # --- fixture workspace ------------------------------------------------
    sample_src = REPO_ROOT / "tests" / "fixtures" / "sample-repo"
    project_root = workspace / "acme-web-e2e"
    shutil.copytree(sample_src, project_root)

    failures = 0

    _banner("1. Bootstrap")
    org_id, user_id, project_id = new_id("org"), new_id("usr"), new_id("prj")
    with session_scope() as session:
        session.add(OrgRow(id=org_id, name="Selfcheck Org"))
        session.add(UserRow(id=user_id, org_id=org_id, email="qa@example.com", role="lead"))
        session.add(
            ProjectRow(
                id=project_id, org_id=org_id, name="acme-web-e2e",
                repository_path=str(project_root),
                base_url="http://127.0.0.1:59999",     # deliberately dead: exercises degradation
                framework="playwright-bdd-pom", language="typescript",
                per_run_cost_limit_usd=5.0,
            )
        )
    _ok("organization, user and project registered", project_root.name)

    # --- run --------------------------------------------------------------
    _banner("2. Full run with human approval gates")
    engine = AgentEngine(offline=not live)
    events: list[tuple[str, str]] = []
    run_id = engine.create_run(
        RunRequest(
            project_id=project_id,
            instruction="Automate the Resident Registration functionality",
            mode=RunMode.FULL,
        ),
        user_id=user_id,
        org_id=org_id,
    )
    engine.subscribe(run_id, lambda e: events.append((e.type, e.message)))
    _ok("run created", run_id)

    gates: list[str] = []
    result = await engine.execute(run_id)
    for _ in range(10):
        if result.status != RunStatus.WAITING_APPROVAL:
            break
        approvals = engine.pending_approvals(run_id=run_id)
        current = next((a for a in approvals if a["id"] == result.approval_id), None)
        if current is None:
            _fail("approval was requested but not persisted", result.approval_id)
            failures += 1
            break
        gates.append(current["kind"])
        print(f"  -> gate: {current['kind']} (risk={current['risk']}) — {current['title'][:70]}")
        print(f"           diff preview: {len(current['diff_preview'])} chars")
        engine.respond_to_approval(result.approval_id, approved=True, user_id=user_id, comment="selfcheck")
        result = await engine.execute(run_id)

    if gates:
        _ok(f"{len(gates)} approval gate(s) exercised", " -> ".join(gates))
    else:
        _fail("no approval gate was raised — human-in-the-loop is not wired")
        failures += 1
    if "test_plan" not in gates:
        _fail("the test plan was not gated for human approval")
        failures += 1
    if "code_write" not in gates:
        _fail("file writes were not gated for human approval")
        failures += 1

    # --- assertions on the persisted run ---------------------------------
    _banner("3. Persisted run state")
    with session_scope() as session:
        row = session.get(RunRow, run_id)
        assert row is not None
        print(f"  status={row.status}  visited={' -> '.join((row.metadata_json or {}).get('visited', []))}")
        _ok("requirement stored", (row.requirement or {}).get("title"))
        _ok("test plan stored", f"{sum(len(f.get('scenarios', [])) for f in (row.test_plan or {}).get('features', []))} scenarios")
        _ok("code bundle stored", f"{len((row.code_bundle or {}).get('changes', []))} files")
        _ok("standards report stored", f"{len((row.standards_report or {}).get('violations', []))} violations")
        _ok("report stored", (row.report or {}).get("headline", "")[:80])
        _ok("cost", f"${row.total_cost_usd:.6f} over {row.llm_calls} LLM call(s), {row.total_tokens} tokens")

        traces = list(session.execute(
            AgentTraceRow.__table__.select().where(AgentTraceRow.run_id == run_id)
        ))
        llm_calls = list(session.execute(
            LLMCallRow.__table__.select().where(LLMCallRow.run_id == run_id)
        ))
        tool_calls = list(session.execute(
            ToolCallRow.__table__.select().where(ToolCallRow.run_id == run_id)
        ))
        audits = list(session.execute(
            AuditRow.__table__.select().where(AuditRow.run_id == run_id)
        ))
        approvals = list(session.execute(
            ApprovalRow.__table__.select().where(ApprovalRow.run_id == run_id)
        ))

        for label, rows, minimum in (
            ("agent traces", traces, 5),
            ("llm call traces", llm_calls, 1),
            ("tool call traces", tool_calls, 3),
            ("audit entries", audits, 2),
            ("approval records", approvals, 2),
        ):
            if len(rows) >= minimum:
                _ok(label, len(rows))
            else:
                _fail(f"{label}: expected >= {minimum}", len(rows))
                failures += 1

        if row.report is None:
            _fail("no report was produced")
            failures += 1
        if row.requirement is None or not (row.requirement or {}).get("acceptance_criteria"):
            _fail("requirement analysis produced no acceptance criteria")
            failures += 1

    # --- assertions on the workspace -------------------------------------
    _banner("4. Files written to the workspace")
    original = {
        p.relative_to(sample_src).as_posix()
        for p in sample_src.rglob("*")
        if p.is_file()
    }
    written = sorted(
        p.relative_to(project_root).as_posix()
        for p in project_root.rglob("*")
        if p.is_file() and p.suffix in (".feature", ".ts", ".json") and "node_modules" not in p.parts
    )
    # Anything not present in the pristine fixture was produced by this run.
    generated = [p for p in written if p not in original]
    for path in generated:
        size = (project_root / path).stat().st_size
        print(f"    {path}  ({size} B)")
    if generated:
        _ok(f"{len(generated)} generated artifact(s) on disk")
    else:
        _fail("no generated files reached the workspace")
        failures += 1

    feature_files = [p for p in generated if p.endswith(".feature")]
    if feature_files:
        content = (project_root / feature_files[0]).read_text(encoding="utf-8")
        print("\n  --- generated feature file ---")
        for line in content.splitlines()[:24]:
            print(f"  | {line}")
        if "Feature:" in content and "Scenario" in content:
            _ok("feature file is valid Gherkin")
        else:
            _fail("feature file is not valid Gherkin")
            failures += 1
        if "@" not in content:
            _fail("generated scenarios carry no tags (violates the org standard)")
            failures += 1
    else:
        _fail("no feature file was generated")
        failures += 1

    # --- safety checks ----------------------------------------------------
    _banner("5. Safety and governance")
    from packages.security import PolicyViolation, WorkspaceGuard

    guard = WorkspaceGuard(project_root)
    for label, fn in (
        (".env read blocked", lambda: guard.resolve_read(".env")),
        ("source write blocked", lambda: guard.resolve_write("src/index.ts")),
        ("path escape blocked", lambda: guard.resolve_read("../../etc/passwd")),
    ):
        try:
            fn()
            _fail(label)
            failures += 1
        except PolicyViolation as exc:
            _ok(label, exc.rule)

    from packages.security import redact

    leaked = redact("DB_PASSWORD=hunter2 and sk-ant-abcdefghijklmnopqrstuvwxyz012345")
    if "hunter2" in leaked or "abcdefghijkl" in leaked:
        _fail("secret redaction leaked a value", leaked)
        failures += 1
    else:
        _ok("secret redaction", leaked[:64])

    # --- event stream -----------------------------------------------------
    _banner("6. Live event stream")
    kinds = {t for t, _ in events}
    print(f"  {len(events)} event(s); kinds: {', '.join(sorted(kinds))}")
    for required in ("run_started", "agent_started", "agent_finished", "llm_call", "tool_call"):
        if required in kinds:
            _ok(f"event '{required}'")
        else:
            _fail(f"missing event '{required}'")
            failures += 1

    # --- report -----------------------------------------------------------
    _banner("7. Report")
    with session_scope() as session:
        row = session.get(RunRow, run_id)
        report = (row.report or {}) if row else {}
    print(f"  headline: {report.get('headline', '(none)')}")
    print("  next actions:")
    for action in report.get("next_actions", []):
        print(f"    - {action}")
    markdown = report.get("markdown", "")
    _ok("markdown report", f"{len(markdown)} chars")
    _ok("html report", f"{len(report.get('html', ''))} chars")

    # --- wrap up ----------------------------------------------------------
    _banner("RESULT")
    if failures:
        print(f"  {failures} check(s) FAILED")
    else:
        print("  all checks passed")
    if keep:
        print(f"\n  workspace kept at: {workspace}")
    else:
        reset_db_state()
        shutil.rmtree(workspace, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI QA Engineer end-to-end self-check")
    parser.add_argument("--live", action="store_true", help="use configured LLM providers instead of offline mode")
    parser.add_argument("--keep", action="store_true", help="keep the temporary workspace for inspection")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(live=args.live, keep=args.keep)))
