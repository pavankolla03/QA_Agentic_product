"""Per-agent token profile for a single run — the before/after ruler for cost work.

    python -m scripts.profile_run
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


async def profile(mode: str = "generate", label: str = "") -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="aiqa-profile-"))
    os.environ["AIQA_DATABASE_URL"] = f"sqlite:///{(tmp / 'p.db').as_posix()}"
    os.environ["AIQA_ARTIFACTS_DIR"] = str(tmp / "a")
    os.environ["AIQA_PER_RUN_COST_LIMIT_USD"] = "5.0"

    from configs.settings import reset_config_cache

    reset_config_cache()

    from sqlalchemy import select

    from packages.aiqa_types.enums import RunMode
    from packages.aiqa_types.models import RunRequest, new_id
    from scripts.demo_app import DemoApp
    from services.agent_engine.engine import AgentEngine
    from services.observability.db import init_db, reset_db_state, session_scope
    from services.observability.models import LLMCallRow, OrgRow, ProjectRow, RunRow, UserRow

    reset_db_state()
    init_db()
    repo = tmp / "repo"
    shutil.copytree(REPO_ROOT / "tests" / "fixtures" / "sample-repo", repo)
    for command in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.email=a@a", "-c", "user.name=a", "commit", "-q", "-m", "i"]):
        subprocess.run(command, cwd=repo, capture_output=True)

    org, user, project = new_id("org"), new_id("usr"), new_id("prj")
    with DemoApp() as app:
        with session_scope() as session:
            session.add(OrgRow(id=org, name="p"))
            session.add(UserRow(id=user, org_id=org, email="a@a"))
            session.add(ProjectRow(id=project, org_id=org, name="p", repository_path=str(repo),
                                   base_url=app.base_url, per_run_cost_limit_usd=5.0))
        engine = AgentEngine(offline=True)
        run_id = engine.create_run(
            RunRequest(project_id=project, instruction="Automate the Resident Registration functionality",
                       mode=RunMode(mode), auto_approve=True, max_cost_usd=5.0),
            user_id=user, org_id=org,
        )
        await engine.run_to_completion(run_id, auto_approve=True)
        with session_scope() as session:
            calls = list(session.execute(select(LLMCallRow).where(LLMCallRow.run_id == run_id)).scalars())
            row = session.get(RunRow, run_id)
            scenarios = sum(len(f.get("scenarios", [])) for f in (row.test_plan or {}).get("features", []))
            files = row.files_changed

    by_agent: dict[str, dict[str, int]] = {}
    for call in calls:
        entry = by_agent.setdefault(call.agent or "?", {"calls": 0, "in": 0, "out": 0, "tier": call.capability})
        entry["calls"] += 1
        entry["in"] += call.prompt_tokens
        entry["out"] += call.completion_tokens

    total_in = sum(e["in"] for e in by_agent.values())
    total_out = sum(e["out"] for e in by_agent.values())

    print(f"\n{'=' * 72}\n{label or mode} profile\n{'=' * 72}")
    print(f"  {'agent':<20} {'tier':<11} {'calls':>5} {'in':>8} {'out':>8}")
    print(f"  {'-' * 56}")
    for agent, entry in sorted(by_agent.items(), key=lambda kv: -kv[1]["in"]):
        print(f"  {agent:<20} {entry['tier']:<11} {entry['calls']:>5} {entry['in']:>8,} {entry['out']:>8,}")
    print(f"  {'-' * 56}")
    print(f"  {'TOTAL':<20} {'':<11} {len(calls):>5} {total_in:>8,} {total_out:>8,}")
    print(f"  scenarios: {scenarios}   files: {files}   "
          f"tokens/scenario: {(total_in + total_out) / scenarios if scenarios else 0:,.0f}")

    reset_db_state()
    shutil.rmtree(tmp, ignore_errors=True)
    return {"calls": len(calls), "in": total_in, "out": total_out,
            "scenarios": scenarios, "files": files, "by_agent": by_agent}


if __name__ == "__main__":
    asyncio.run(profile(sys.argv[1] if len(sys.argv) > 1 else "generate"))
