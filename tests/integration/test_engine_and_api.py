"""Run lifecycle, human-in-the-loop gates, durability, and the HTTP surface."""

from __future__ import annotations

from packages.aiqa_types.enums import RunMode, RunStatus
from packages.aiqa_types.models import RunRequest


# =========================================================================== #
# Engine: gates, resume, durability
# =========================================================================== #
async def test_run_pauses_at_the_test_plan_gate(engine, project, org_user) -> None:
    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(project_id=project.id, instruction="Automate resident registration", mode=RunMode.FULL),
        user_id=user_id, org_id=org_id,
    )
    result = await engine.execute(run_id)

    assert result.status == RunStatus.WAITING_APPROVAL
    assert result.suspended_at == "test_design"
    pending = engine.pending_approvals(run_id=run_id)
    assert [a["kind"] for a in pending] == ["test_plan"]
    assert pending[0]["diff_preview"], "the reviewer must be shown the Gherkin"


def _user_files(root) -> set[str]:
    """Everything in the repository except the platform's own `.aiqa/` cache.

    `.aiqa/` holds the repository and application maps — platform bookkeeping that
    is written during exploration and is what makes later runs cheap. The safety
    property is about the user's *test assets*, so it is asserted over everything
    outside that directory.
    """
    return {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not p.relative_to(root).as_posix().startswith(".aiqa/")
    }


async def test_no_test_assets_are_written_before_approval(engine, project, org_user, repo_copy) -> None:
    """The core safety property: a pending gate means untouched user code."""
    before = _user_files(repo_copy)
    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(project_id=project.id, instruction="Automate resident registration", mode=RunMode.FULL),
        user_id=user_id, org_id=org_id,
    )
    await engine.execute(run_id)
    assert _user_files(repo_copy) == before


async def test_only_the_knowledge_cache_may_be_written_before_approval(
    engine, project, org_user, repo_copy
) -> None:
    """Whatever the platform does write pre-approval must be confined to `.aiqa/`."""
    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(project_id=project.id, instruction="Automate resident registration", mode=RunMode.FULL),
        user_id=user_id, org_id=org_id,
    )
    await engine.execute(run_id)

    written = {
        p.relative_to(repo_copy).as_posix()
        for p in repo_copy.rglob("*")
        if p.is_file() and p.relative_to(repo_copy).as_posix().startswith(".aiqa/")
    }
    # Only knowledge caches and the standards file the fixture ships with.
    assert written <= {
        ".aiqa/standards.yaml",
        ".aiqa/repository_map.json",
        ".aiqa/application_map.json",
    }, written


async def test_rejecting_a_gate_cancels_the_run(engine, project, org_user, repo_copy) -> None:
    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(project_id=project.id, instruction="Automate resident registration", mode=RunMode.FULL),
        user_id=user_id, org_id=org_id,
    )
    result = await engine.execute(run_id)
    engine.respond_to_approval(result.approval_id, approved=False, user_id=user_id, comment="wrong scope")

    from services.observability.db import session_scope
    from services.observability.models import RunRow

    with session_scope() as session:
        row = session.get(RunRow, run_id)
        assert row.status == RunStatus.CANCELLED.value
        assert "rejected" in row.error

    generated = [p for p in repo_copy.rglob("*resident*") if p.is_file()]
    assert not generated, "a rejected run must leave nothing behind"


async def test_approving_resumes_at_the_same_node(engine, project, org_user) -> None:
    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(project_id=project.id, instruction="Automate resident registration", mode=RunMode.FULL),
        user_id=user_id, org_id=org_id,
    )
    first = await engine.execute(run_id)
    engine.respond_to_approval(first.approval_id, approved=True, user_id=user_id)
    second = await engine.execute(run_id)

    assert second.visited[0] == "test_design", "resume must re-enter the node that asked"
    assert second.status in (RunStatus.WAITING_APPROVAL, RunStatus.SUCCEEDED)


async def test_a_suspended_run_survives_a_fresh_engine(project, org_user) -> None:
    """Durability: approval can arrive after a restart, with no in-memory state."""
    from services.agent_engine.engine import AgentEngine

    org_id, user_id = org_user
    first_engine = AgentEngine(offline=True)
    run_id = first_engine.create_run(
        RunRequest(project_id=project.id, instruction="Automate resident registration", mode=RunMode.FULL),
        user_id=user_id, org_id=org_id,
    )
    paused = await first_engine.execute(run_id)
    assert paused.status == RunStatus.WAITING_APPROVAL

    # Simulate a process restart: brand-new engine, nothing carried over.
    second_engine = AgentEngine(offline=True)
    second_engine.respond_to_approval(paused.approval_id, approved=True, user_id=user_id)
    resumed = await second_engine.execute(run_id)

    assert resumed.status in (RunStatus.WAITING_APPROVAL, RunStatus.SUCCEEDED)
    from services.observability.db import session_scope
    from services.observability.models import RunRow

    with session_scope() as session:
        row = session.get(RunRow, run_id)
        assert row.test_plan is not None, "the plan from before the restart must be reused"
        assert row.requirement is not None


async def test_auto_approve_completes_without_a_human(engine, project, org_user, repo_copy) -> None:
    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(
            project_id=project.id, instruction="Automate resident registration",
            mode=RunMode.FULL, auto_approve=True,
        ),
        user_id=user_id, org_id=org_id,
    )
    result = await engine.run_to_completion(run_id, auto_approve=True)
    assert result.status == RunStatus.SUCCEEDED
    assert engine.pending_approvals(run_id=run_id) == []

    written = [p for p in repo_copy.rglob("*resident*") if p.is_file()]
    assert written, "auto-approved runs should write their generated files"


async def test_plan_only_mode_never_generates_code(engine, project, org_user, repo_copy) -> None:
    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(project_id=project.id, instruction="Automate resident registration", mode=RunMode.PLAN_ONLY),
        user_id=user_id, org_id=org_id,
    )
    result = await engine.run_to_completion(run_id, auto_approve=True)
    assert result.status == RunStatus.SUCCEEDED
    assert "code_generation" not in result.visited

    from services.observability.db import session_scope
    from services.observability.models import RunRow

    with session_scope() as session:
        row = session.get(RunRow, run_id)
        assert row.test_plan is not None
        assert row.code_bundle is None
    assert not [p for p in repo_copy.rglob("*resident*") if p.is_file()]


async def test_cost_and_traces_are_recorded(engine, project, org_user) -> None:
    from sqlalchemy import select

    from services.observability.db import session_scope
    from services.observability.models import AgentTraceRow, LLMCallRow, RunRow, ToolCallRow

    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(project_id=project.id, instruction="Automate resident registration",
                   mode=RunMode.FULL, auto_approve=True),
        user_id=user_id, org_id=org_id,
    )
    await engine.run_to_completion(run_id, auto_approve=True)

    with session_scope() as session:
        row = session.get(RunRow, run_id)
        traces = list(session.execute(select(AgentTraceRow).where(AgentTraceRow.run_id == run_id)).scalars())
        llm = list(session.execute(select(LLMCallRow).where(LLMCallRow.run_id == run_id)).scalars())
        tools = list(session.execute(select(ToolCallRow).where(ToolCallRow.run_id == run_id)).scalars())

    assert len(traces) >= 5
    assert len(llm) >= 1
    assert len(tools) >= 3
    assert row.llm_calls == len([c for c in llm if c.status == "succeeded"])
    # Every trace carries the full traceability tuple the spec requires.
    for trace in traces:
        assert trace.run_id and trace.project_id and trace.session_id and trace.agent


async def test_events_are_persisted_for_replay(engine, project, org_user) -> None:
    from sqlalchemy import select

    from services.observability.db import session_scope
    from services.observability.models import RunEventRow

    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(project_id=project.id, instruction="Automate resident registration",
                   mode=RunMode.GENERATE, auto_approve=True),
        user_id=user_id, org_id=org_id,
    )
    await engine.run_to_completion(run_id, auto_approve=True)

    with session_scope() as session:
        events = list(session.execute(select(RunEventRow).where(RunEventRow.run_id == run_id)).scalars())
    kinds = {e.type for e in events}
    assert {"run_started", "agent_started", "agent_finished", "llm_call"} <= kinds


async def test_live_subscribers_receive_events(engine, project, org_user) -> None:
    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(project_id=project.id, instruction="Automate resident registration", mode=RunMode.PLAN_ONLY),
        user_id=user_id, org_id=org_id,
    )
    received: list = []
    unsubscribe = engine.subscribe(run_id, received.append)
    await engine.execute(run_id)
    unsubscribe()

    assert received
    assert any(e.type == "agent_started" for e in received)


# =========================================================================== #
# HTTP API
# =========================================================================== #
def test_health_needs_no_auth(api_client) -> None:
    response = api_client.get("/api/health", headers={"X-API-Key": ""})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_endpoints_require_a_key(api_client) -> None:
    response = api_client.get("/api/projects", headers={"X-API-Key": ""})
    assert response.status_code == 401


def test_a_bad_key_is_rejected(api_client) -> None:
    response = api_client.get("/api/projects", headers={"X-API-Key": "nope"})
    assert response.status_code == 401


def test_project_crud(api_client, repo_copy) -> None:
    created = api_client.post(
        "/api/projects",
        json={"name": "api-project", "repository_path": str(repo_copy), "base_url": "http://localhost:3000"},
    )
    assert created.status_code == 201
    project_id = created.json()["id"]

    assert api_client.get(f"/api/projects/{project_id}").status_code == 200
    assert any(p["id"] == project_id for p in api_client.get("/api/projects").json())

    patched = api_client.patch(f"/api/projects/{project_id}", json={"base_url": "http://localhost:4000"})
    assert patched.json()["base_url"] == "http://localhost:4000"

    assert api_client.delete(f"/api/projects/{project_id}").status_code == 204
    assert api_client.get(f"/api/projects/{project_id}").status_code == 404


def test_a_connection_string_is_refused_as_a_dsn_ref(api_client, repo_copy) -> None:
    """Projects reference a secret by env-var NAME; a literal DSN must be rejected."""
    response = api_client.post(
        "/api/projects",
        json={
            "name": "leaky", "repository_path": str(repo_copy),
            "database_dsn_ref": "postgres://user:password@host:5432/db",
        },
    )
    assert response.status_code == 422


def test_a_missing_repository_path_is_refused(api_client) -> None:
    response = api_client.post(
        "/api/projects", json={"name": "ghost", "repository_path": "C:/definitely/not/here"}
    )
    assert response.status_code == 400


def test_duplicate_project_names_are_refused(api_client, repo_copy) -> None:
    payload = {"name": "dupe", "repository_path": str(repo_copy)}
    assert api_client.post("/api/projects", json=payload).status_code == 201
    assert api_client.post("/api/projects", json=payload).status_code == 409


def test_indexing_and_linting_over_http(api_client, repo_copy) -> None:
    project_id = api_client.post(
        "/api/projects", json={"name": "indexed", "repository_path": str(repo_copy)}
    ).json()["id"]

    indexed = api_client.post(f"/api/projects/{project_id}/index").json()
    assert indexed["files"] > 0
    assert indexed["test_runner"] == "playwright"
    assert "LoginPage" in indexed["page_objects"]

    linted = api_client.post(f"/api/projects/{project_id}/lint").json()
    assert linted["files_checked"] > 0
    assert linted["errors"] == 0, linted["violations"]


def test_standards_merge_the_project_override(api_client, repo_copy) -> None:
    project_id = api_client.post(
        "/api/projects", json={"name": "std", "repository_path": str(repo_copy)}
    ).json()["id"]
    standards = api_client.get(f"/api/projects/{project_id}/standards").json()
    assert "Acme" in standards["organization"], "the project's .aiqa/standards.yaml should win"
    rule_ids = {r["id"] for r in standards["standards"]["rules"]}
    assert "ACME-001" in rule_ids, "project-specific rules must be merged in"
    assert "STD-001" in rule_ids, "organization rules must be retained"


def test_full_run_through_the_api(api_client, repo_copy) -> None:
    project_id = api_client.post(
        "/api/projects",
        json={"name": "runner", "repository_path": str(repo_copy), "base_url": "http://127.0.0.1:59999"},
    ).json()["id"]

    created = api_client.post(
        "/api/runs",
        json={"project_id": project_id, "instruction": "Automate resident registration",
              "mode": "full", "auto_approve": True, "start": False},
    )
    assert created.status_code == 201
    run_id = created.json()["id"]

    # Drive it synchronously so the test does not depend on background timing.
    import asyncio

    import services.api_gateway.app as app_module

    asyncio.get_event_loop_policy().new_event_loop()
    result = asyncio.run(app_module.app.state.engine.run_to_completion(run_id, auto_approve=True))
    assert result.status == RunStatus.SUCCEEDED

    detail = api_client.get(f"/api/runs/{run_id}").json()
    assert detail["scenarios"] > 0
    assert detail["files_changed"] > 0
    assert detail["report"]["headline"]
    assert len(detail["traces"]) >= 5

    assert "Feature:" in api_client.get(f"/api/runs/{run_id}/diff").text
    assert api_client.get(f"/api/runs/{run_id}/events").json()["count"] > 0
    trace = api_client.get(f"/api/runs/{run_id}/trace").json()
    assert trace["llm_calls"] and trace["tool_calls"] and trace["audit"]
    assert "# AI QA run" in api_client.get(f"/api/runs/{run_id}/report?format=markdown").text


def test_rbac_is_enforced_over_http(api_client) -> None:
    issued = api_client.post("/api/keys", json={"name": "engineer-key", "role": "engineer"})
    assert issued.status_code == 201
    engineer_key = issued.json()["api_key"]

    headers = {"X-API-Key": engineer_key}
    assert api_client.get("/api/projects", headers=headers).status_code == 200
    # Issuing keys is admin-only.
    assert api_client.post("/api/keys", json={"name": "x"}, headers=headers).status_code == 403


def test_api_keys_are_never_stored_in_plaintext(api_client) -> None:
    from sqlalchemy import select

    from services.observability.db import session_scope
    from services.observability.models import ApiKeyRow

    raw = api_client.post("/api/keys", json={"name": "secret-key"}).json()["api_key"]
    with session_scope() as session:
        rows = list(session.execute(select(ApiKeyRow)).scalars())
    assert all(row.key_hash != raw for row in rows)
    assert not any(raw in (row.key_hash or "") for row in rows)


def test_metrics_and_audit_are_available(api_client, repo_copy) -> None:
    api_client.post("/api/projects", json={"name": "metrics", "repository_path": str(repo_copy)})
    metrics = api_client.get("/api/metrics?days=30").json()
    assert "runs" in metrics and "cost" in metrics and "agents" in metrics
    assert "governance" in metrics["cost"]

    audit = api_client.get("/api/audit").json()
    assert audit["count"] >= 1
    assert any(entry["action"] == "project_create" for entry in audit["entries"])


def test_agents_and_tools_are_introspectable(api_client) -> None:
    agents = api_client.get("/api/agents").json()
    assert len(agents["agents"]) == 10
    assert set(agents["modes"]) >= {"plan_only", "generate", "full", "autonomous"}

    tools = api_client.get("/api/tools").json()
    assert tools["count"] >= 25
    names = {t["name"] for t in tools["tools"]}
    assert {"fs.write_file", "git.commit", "playwright.run_tests", "db.query"} <= names


async def test_generate_mode_writes_files_but_does_not_execute(engine, project, org_user, repo_copy) -> None:
    """`generate` must produce usable files on disk; only the test *run* is skipped."""
    org_id, user_id = org_user
    run_id = engine.create_run(
        RunRequest(project_id=project.id, instruction="Automate resident registration",
                   mode=RunMode.GENERATE, auto_approve=True),
        user_id=user_id, org_id=org_id,
    )
    result = await engine.run_to_completion(run_id, auto_approve=True)
    assert result.status == RunStatus.SUCCEEDED

    written = [p for p in repo_copy.rglob("*resident*") if p.is_file()]
    assert written, "generate mode produced no files"

    from services.observability.db import session_scope
    from services.observability.models import RunRow

    with session_scope() as session:
        row = session.get(RunRow, run_id)
        assert row.code_bundle is not None
        assert row.tests_total == 0, "generate mode must not execute the suite"


# =========================================================================== #
# Orchestrator backends must be interchangeable
# =========================================================================== #
async def test_langgraph_and_builtin_orchestrators_agree(project, org_user, repo_copy) -> None:
    """Both backends drive the same agents, so they must reach the same result."""
    from agents.orchestrator.graph import Orchestrator, build_default_nodes
    from agents.orchestrator.langgraph_graph import LANGGRAPH_AVAILABLE, LangGraphOrchestrator
    from services.agent_engine.engine import AgentEngine
    from services.observability.db import session_scope
    from services.observability.models import RunRow

    if not LANGGRAPH_AVAILABLE:
        import pytest

        pytest.skip("langgraph is not installed")

    from packages.aiqa_types.models import new_id
    from services.observability.models import ProjectRow

    org_id, user_id = org_user
    outcomes = {}
    for label, orchestrator in (
        ("builtin", Orchestrator(build_default_nodes())),
        ("langgraph", LangGraphOrchestrator(build_default_nodes())),
    ):
        # Each backend gets its own project id, and therefore its own Test
        # Knowledge Store. Sharing one would make the second run correctly
        # dedupe against the first and look like a divergence.
        project_id = new_id("prj")
        with session_scope() as session:
            session.add(
                ProjectRow(
                    id=project_id, org_id=org_id, name=f"equivalence-{label}",
                    repository_path=str(repo_copy), base_url="http://127.0.0.1:59999",
                )
            )

        engine = AgentEngine(orchestrator=orchestrator, offline=True)
        run_id = engine.create_run(
            RunRequest(project_id=project_id, instruction="Automate resident registration",
                       mode=RunMode.PLAN_ONLY, auto_approve=True),
            user_id=user_id, org_id=org_id,
        )
        result = await engine.run_to_completion(run_id, auto_approve=True)
        with session_scope() as session:
            row = session.get(RunRow, run_id)
            scenarios = sum(len(f.get("scenarios", [])) for f in (row.test_plan or {}).get("features", []))
            has_report = row.report is not None
        outcomes[label] = (result.status, scenarios, has_report)

    assert outcomes["builtin"] == outcomes["langgraph"], outcomes


async def test_langgraph_honours_approval_gates(project, org_user) -> None:
    from agents.orchestrator.graph import build_default_nodes
    from agents.orchestrator.langgraph_graph import LANGGRAPH_AVAILABLE, LangGraphOrchestrator
    from services.agent_engine.engine import AgentEngine

    if not LANGGRAPH_AVAILABLE:
        import pytest

        pytest.skip("langgraph is not installed")

    org_id, user_id = org_user
    engine = AgentEngine(orchestrator=LangGraphOrchestrator(build_default_nodes()), offline=True)
    run_id = engine.create_run(
        RunRequest(project_id=project.id, instruction="Automate resident registration", mode=RunMode.FULL),
        user_id=user_id, org_id=org_id,
    )
    result = await engine.execute(run_id)
    assert result.status == RunStatus.WAITING_APPROVAL
    assert result.suspended_at == "test_design"

    engine.respond_to_approval(result.approval_id, approved=True, user_id=user_id)
    resumed = await engine.execute(run_id)
    assert resumed.visited[0] == "test_design", "resume must re-enter the suspending node"
