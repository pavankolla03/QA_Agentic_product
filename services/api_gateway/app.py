"""QAgentic Control Plane — FastAPI application.

The single ingress for the VS Code extension, the web dashboard and CI. Every
route is authenticated, RBAC-checked and audited; every run is observable while
it happens (WebSocket) and after it finishes (persisted event log).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, delete, desc, func, select

from agents.orchestrator.graph import describe_agents
from configs.settings import get_settings, load_project_standards
from packages.aiqa_types.enums import ApprovalStatus, AuditAction, RunMode, RunStatus
from packages.aiqa_types.models import Project, RunRequest, new_id
from packages.security.guard import PolicyViolation
from services.agent_engine.conversation import ConversationService
from services.agent_engine.engine import AgentEngine, RunNotFound, get_engine
from services.agent_engine.intents import resolve
from services.api_gateway.auth import (
    CurrentPrincipal,
    Principal,
    bootstrap_admin,
    create_api_key,
    requires,
    revoke_api_key,
)
from services.api_gateway.schemas import (
    ApiKeyCreate,
    ApprovalDecision,
    ApprovalOut,
    ChatIn,
    ChatOut,
    HealthOut,
    LintRequest,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    RunCreate,
    RunDetail,
    RunSummary,
)
from services.model_router.router import ModelRouter
from services.observability.db import init_db, session_scope
from services.observability.models import (
    AgentTraceRow,
    ApiKeyRow,
    ApprovalRow,
    ArtifactRow,
    AuditRow,
    CostDailyRow,
    FlakyTestRow,
    HealHistoryRow,
    KnowledgeChunkRow,
    LLMCallRow,
    ProjectRow,
    RunEventRow,
    RunRow,
    ToolCallRow,
    UserRow,
)
from services.observability.tracker import CostGovernor, RunTracker

log = logging.getLogger("aiqa.api")

VERSION = "0.1.0"
CONTROL_PLANE_DIR = Path(__file__).resolve().parent.parent.parent / "apps" / "control-plane"


# =========================================================================== #
# Lifespan
# =========================================================================== #
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    info = bootstrap_admin()
    app.state.engine = get_engine()
    # Nothing is in flight at startup, so anything the database still calls
    # running is a leftover from a process that is gone.
    app.state.engine.reconcile_interrupted_runs()
    app.state.router = ModelRouter()
    if info.get("created") == "true":
        log.warning(
            "bootstrapped org=%s user=%s. API key comes from AIQA_BOOTSTRAP_API_KEY — change it before sharing.",
            info["org_id"], info["user_id"],
        )
    log.info("QAgentic control plane ready on %s:%s (env=%s)", settings.host, settings.port, settings.env)
    yield
    with contextlib.suppress(Exception):
        await app.state.router.close()


app = FastAPI(
    title="QAgentic — Control Plane",
    description=(
        "Autonomous QA engineering platform. The LLM reasons; deterministic tools act; "
        "a human approves anything that touches the workspace."
    ),
    version=VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # The extension talks to localhost; a deployment should narrow this.
    allow_origins=["http://localhost", "http://127.0.0.1", "vscode-webview://*"],
    allow_origin_regex=r"^(http://(localhost|127\.0\.0\.1)(:\d+)?|vscode-webview://.*)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(PolicyViolation)
async def _policy_handler(_request: Request, exc: PolicyViolation) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": exc.message, "rule": exc.rule, "resource": exc.resource})


@app.exception_handler(RunNotFound)
async def _not_found_handler(_request: Request, exc: RunNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


api = APIRouter(prefix="/api")


def _engine() -> AgentEngine:
    return app.state.engine if hasattr(app.state, "engine") else get_engine()


def _router() -> ModelRouter:
    if not hasattr(app.state, "router"):
        app.state.router = ModelRouter()
    return app.state.router


def _iso(value: datetime | None) -> str:
    """Serialize a timestamp as unambiguous UTC.

    SQLite returns naive datetimes even for columns declared timezone-aware.
    Emitting them without an offset makes a browser parse UTC as local time,
    which showed freshly-created runs as hours old.
    """
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


# =========================================================================== #
# Health & metadata
# =========================================================================== #
@api.get("/health/live", tags=["system"])
async def health_live() -> dict[str, str]:
    """Is this process alive? Nothing else.

    Deliberately touches no database, no Redis and no provider. A liveness
    probe that can be made slow by a third party is not a liveness probe — it
    is a way to have a healthy process restarted because somebody else's API
    was busy.
    """
    return {"status": "ok", "version": VERSION}


@api.get("/health/ready", tags=["system"])
async def health_ready() -> JSONResponse:
    """Can this process actually serve? Database and queue only.

    Model providers are excluded on purpose: the platform is still useful with
    every provider down — deterministic answers, run history, cost queries and
    the whole execution layer keep working.
    """
    checks: dict[str, str] = {}
    try:
        with session_scope() as session:
            session.execute(select(func.count(RunRow.id)).limit(1)).scalar_one()
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"unavailable: {str(exc)[:120]}"

    # The queue is reported but never makes this endpoint fail. An in-process
    # queue is a perfectly serviceable deployment — it is the default — and a
    # readiness probe that rejects it would refuse traffic to a working
    # install. What matters is that nobody has to guess which one they have.
    try:
        queue = await _engine().queue()
        health = await queue.health()
        checks["queue"] = health.summary()
        durable = health.durable and health.reachable
    except Exception as exc:  # noqa: BLE001
        checks["queue"] = f"unavailable: {str(exc)[:120]}"
        durable = False

    ready = checks.get("database") == "ok"
    return JSONResponse(
        {
            "status": "ready" if ready else "not_ready",
            "checks": checks,
            "durable_runs": durable,
        },
        status_code=200 if ready else 503,
    )




@api.get("/health/providers", tags=["system"])
async def health_providers() -> dict[str, Any]:
    """Model provider diagnostics — the expensive one, asked for explicitly.

    This probes upstream APIs, so it belongs behind its own URL rather than
    inside a request somebody is waiting on. Ordinary chat must never pay for
    it, which is the entire reason it is not part of `/health/live`.
    """
    router = _router()
    try:
        snapshot = await router.status()
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detail": str(exc)[:300], "providers": {}}
    return {
        "status": "ok",
        "active_routes": snapshot.get("active_routes", {}),
        "providers": snapshot.get("providers", {}),
        "free_only": router.free_only,
    }


@api.get("/health", response_model=HealthOut, tags=["system"])
async def health() -> HealthOut:
    settings = get_settings()
    router = _router()
    try:
        snapshot = await router.status()
        routes = snapshot["active_routes"]
    except Exception:  # noqa: BLE001
        routes = {}
    return HealthOut(
        status="ok",
        version=VERSION,
        env=settings.env,
        database=settings.database_url.split("://")[0],
        configured_providers=settings.configured_providers,
        active_routes=routes,
        cost=CostGovernor().snapshot(),
    )


@api.get("/agents", tags=["system"])
async def agents(_p: CurrentPrincipal) -> dict[str, Any]:
    from agents.orchestrator.graph import Orchestrator
    from packages.aiqa_types.enums import RunMode

    orchestrator = Orchestrator()
    return {
        "agents": describe_agents(),
        "modes": {mode.value: orchestrator.plan_for(mode) for mode in RunMode},
    }


@api.get("/providers", tags=["system"])
async def providers(_p: CurrentPrincipal) -> dict[str, Any]:
    return await _router().status()


@api.get("/tools", tags=["system"])
async def tools(_p: CurrentPrincipal) -> dict[str, Any]:
    from tools import ToolContext, build_registry

    registry = build_registry(ToolContext(project_root="."))
    return {"count": len(registry), "tools": registry.specs()}


@api.get("/settings", tags=["system"])
async def settings_view(principal: Principal = requires("project:read")) -> dict[str, Any]:
    return get_settings().to_safe_dict()


# =========================================================================== #
# Projects
# =========================================================================== #
def _project_out(row: ProjectRow, indexed: bool = False) -> ProjectOut:
    return ProjectOut(
        id=row.id, org_id=row.org_id, name=row.name, repository_path=row.repository_path,
        repository_url=row.repository_url or "", default_branch=row.default_branch,
        framework=row.framework, language=row.language, base_url=row.base_url or "",
        api_base_url=row.api_base_url or "", database_dsn_ref=row.database_dsn_ref or "",
        tags=row.tags or [], per_run_cost_limit_usd=row.per_run_cost_limit_usd,
        indexed=indexed, created_at=_iso(row.created_at),
    )


@api.post("/projects", response_model=ProjectOut, status_code=201, tags=["projects"])
async def create_project(payload: ProjectCreate, principal: Principal = requires("project:write")) -> ProjectOut:
    root = Path(payload.repository_path).expanduser()
    if not root.exists():
        raise HTTPException(status_code=400, detail=f"repository path does not exist: {root}")
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"repository path is not a directory: {root}")

    with session_scope() as session:
        clash = session.execute(
            select(ProjectRow).where(ProjectRow.org_id == principal.org_id, ProjectRow.name == payload.name)
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(status_code=409, detail=f"a project named '{payload.name}' already exists")

        row = ProjectRow(
            id=new_id("prj"), org_id=principal.org_id, name=payload.name,
            repository_path=str(root.resolve()), repository_url=payload.repository_url or "",
            default_branch=payload.default_branch, framework=payload.framework, language=payload.language,
            base_url=payload.base_url or "", api_base_url=payload.api_base_url or "",
            database_dsn_ref=payload.database_dsn_ref or "",
            standards_override=payload.standards_override, tags=payload.tags,
            per_run_cost_limit_usd=payload.per_run_cost_limit_usd,
        )
        session.add(row)
        session.flush()
        out = _project_out(row)

    RunTracker("", project_id=out.id, org_id=principal.org_id, user_id=principal.user_id).audit(
        AuditAction.PROJECT_CREATE, out.name, "allowed", out.repository_path
    )
    return out


@api.get("/projects", response_model=list[ProjectOut], tags=["projects"])
async def list_projects(principal: Principal = requires("project:read")) -> list[ProjectOut]:
    with session_scope() as session:
        rows = list(
            session.execute(
                select(ProjectRow).where(ProjectRow.org_id == principal.org_id).order_by(ProjectRow.name)
            ).scalars()
        )
        indexed_ids = {
            pid
            for (pid,) in session.execute(
                select(KnowledgeChunkRow.project_id).distinct()
            ).all()
        }
        return [_project_out(row, indexed=row.id in indexed_ids) for row in rows]


@api.get("/projects/{project_id}", response_model=ProjectOut, tags=["projects"])
async def get_project(project_id: str, principal: Principal = requires("project:read")) -> ProjectOut:
    with session_scope() as session:
        row = _owned_project(session, project_id, principal)
        count = session.execute(
            select(func.count()).select_from(KnowledgeChunkRow).where(KnowledgeChunkRow.project_id == project_id)
        ).scalar_one()
        return _project_out(row, indexed=bool(count))


@api.patch("/projects/{project_id}", response_model=ProjectOut, tags=["projects"])
async def update_project(
    project_id: str, payload: ProjectUpdate, principal: Principal = requires("project:write")
) -> ProjectOut:
    with session_scope() as session:
        row = _owned_project(session, project_id, principal)
        for field, value in payload.model_dump(exclude_none=True).items():
            if field == "repository_path":
                candidate = Path(str(value)).expanduser()
                if not candidate.is_dir():
                    raise HTTPException(status_code=400, detail=f"not a directory: {candidate}")
                value = str(candidate.resolve())
            setattr(row, field, value)
        session.flush()
        return _project_out(row)


@api.delete("/projects/{project_id}", status_code=204, tags=["projects"])
async def delete_project(project_id: str, principal: Principal = requires("project:write")) -> None:
    with session_scope() as session:
        row = _owned_project(session, project_id, principal)
        session.execute(delete(KnowledgeChunkRow).where(KnowledgeChunkRow.project_id == project_id))
        session.delete(row)


@api.get("/projects/{project_id}/standards", tags=["projects"])
async def project_standards(project_id: str, principal: Principal = requires("project:read")) -> dict[str, Any]:
    with session_scope() as session:
        row = _owned_project(session, project_id, principal)
        merged = load_project_standards(row.repository_path)
        if row.standards_override:
            merged = {**merged, **row.standards_override}
    return {
        "project_id": project_id,
        "organization": merged.get("organization", ""),
        "source": merged.get("_source", "organization default"),
        "rule_count": len(merged.get("rules", [])),
        "standards": merged,
    }


@api.post("/projects/{project_id}/index", tags=["projects"])
async def index_project(project_id: str, principal: Principal = requires("knowledge:index")) -> dict[str, Any]:
    """Index (or re-index) the repository so agents know its conventions."""
    from services.knowledge_service.indexer import RepositoryIndexer

    with session_scope() as session:
        row = _owned_project(session, project_id, principal)
        repo_path, name = row.repository_path, row.name

    indexer = RepositoryIndexer(project_id, repo_path)
    profile = await indexer.index(router=_router())

    with session_scope() as session:
        row = session.get(ProjectRow, project_id)
        if row is not None:
            payload = profile.model_dump(mode="json")
            payload["symbols"] = payload.get("symbols", [])[:400]
            row.repo_profile = payload
            row.language = profile.language or row.language

    return {
        "project": name,
        "files": profile.file_count,
        "symbols": len(profile.symbols),
        "chunks": profile.indexed_chunks,
        "language": profile.language,
        "test_runner": profile.test_runner,
        "bdd": profile.bdd,
        "layout": profile.detected_layout,
        "naming": profile.naming_conventions,
        "conventions_summary": profile.conventions_summary,
        "page_objects": [s.name for s in profile.symbols_of("page_object")],
        "fixtures": [s.name for s in profile.symbols_of("fixture")],
    }


@api.post("/projects/{project_id}/lint", tags=["projects"])
async def lint_project(
    project_id: str, payload: LintRequest | None = None, principal: Principal = requires("project:read")
) -> dict[str, Any]:
    """Audit the *existing* suite against the organization standards."""
    from agents.base import AgentContext
    from agents.standards.agent import lint_existing_repo
    from services.knowledge_service.indexer import RepositoryIndexer

    with session_scope() as session:
        row = _owned_project(session, project_id, principal)
        project = Project(
            id=row.id, org_id=row.org_id, name=row.name, repository_path=row.repository_path,
            language=row.language, framework=row.framework,
        )
        standards = load_project_standards(row.repository_path)
        if row.standards_override:
            standards = {**standards, **row.standards_override}

    profile, _ = RepositoryIndexer(project_id, project.repository_path).scan()
    ctx = AgentContext(run_id="", project=project, instruction="lint", standards=standards)
    ctx.repo_profile = profile
    report = lint_existing_repo(ctx, (payload.paths if payload else None) or None)
    return {
        "project": project.name,
        "files_checked": report.files_checked,
        "rules_applied": report.rules_applied,
        "passed": report.passed,
        "errors": report.error_count,
        "warnings": report.warning_count,
        "violations": [v.model_dump(mode="json") for v in report.violations[:500]],
    }


def _owned_project(session: Any, project_id: str, principal: Principal) -> ProjectRow:
    row = session.get(ProjectRow, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    if row.org_id != principal.org_id and not principal.can("*"):
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    return row


# =========================================================================== #
# Runs
# =========================================================================== #
def _run_summary(row: RunRow, project_name: str = "", pending: str = "") -> RunSummary:
    plan = row.test_plan or {}
    scenarios = sum(len(f.get("scenarios", [])) for f in plan.get("features", []))
    return RunSummary(
        id=row.id, project_id=row.project_id, project_name=project_name,
        instruction=row.instruction, mode=row.mode, status=row.status,
        current_agent=row.current_agent or "", progress=row.progress,
        scenarios=scenarios, files_changed=row.files_changed,
        tests_total=row.tests_total, tests_passed=row.tests_passed, tests_failed=row.tests_failed,
        total_cost_usd=round(row.total_cost_usd, 6), total_tokens=row.total_tokens,
        llm_calls=row.llm_calls, duration_s=round(row.duration_s, 2), error=row.error,
        pending_approval_id=pending, created_at=_iso(row.created_at), ended_at=_iso(row.ended_at),
    )


@api.post("/chat", response_model=ChatOut, tags=["runs"])
async def chat(payload: ChatIn, principal: Principal = requires("run:read")) -> ChatOut:
    """Answer a chat message, or say that it warrants a run.

    Three tiers, in order, and most messages never leave the first two:

    1. **A named intent with a deterministic answer.** "How many tests failed?"
       is a database row, not a question for a model. Answering it by query is
       faster, cheaper, correct, and still works when every provider is rate
       limited — the one case where a model is strictly worse than SQL.
    2. **A request for work.** Becomes a run, as it always did.
    3. **Genuinely ambiguous prose.** One short call on the `interactive_chat`
       tier: five-second ceiling, three hundred tokens, no retries.

    The fallback is always to *answer*, never to escalate into a run. A wrong
    sentence costs a sentence; a wrong run costs five minutes and writes files.
    """
    resolution = resolve(payload.message)

    if resolution.intent.starts_a_run:
        return ChatOut(kind="run", mode=resolution.mode)

    project_id = payload.project_id or ""
    answer = ConversationService(project_id=project_id, org_id=principal.org_id).answer(resolution)
    if answer is not None:
        return ChatOut(kind="reply", text=answer.text, suggestions=answer.suggestions or [])

    # Nothing deterministic fits. Ask a model, briefly.
    context = _chat_context(project_id, principal)
    try:
        reply = await _engine().answer(payload.message, context=context)
    except Exception:  # noqa: BLE001 - a chat reply must never fail the panel
        log.debug("chat answer failed", exc_info=True)
        reply = ""

    if reply.strip().upper().startswith("RUN"):
        return ChatOut(kind="run", mode=RunMode.FULL)
    if not reply:
        reply = (
            "I could not reach a model in time to answer that. Ask me about a run, "
            "a failure or today's cost and I will answer from my own records, which "
            "needs no model at all."
        )
    return ChatOut(kind="reply", text=reply)


@api.post("/chat/stream", tags=["runs"])
async def chat_stream(payload: ChatIn, principal: Principal = requires("run:read")):
    """The same decision as `/api/chat`, delivered as it is made.

    Every outcome uses the same frame sequence, so the client has one code path
    rather than three:

        chat_started
        token ...            (deterministic answers arrive as a single token)
        chat_finished        or  run_suggested

    A deterministic answer is already complete when it is produced, so it is
    sent whole. Only the model-backed path genuinely streams — and that is the
    one where four seconds of silence used to look like a broken panel.
    """
    resolution = resolve(payload.message)
    project_id = payload.project_id or ""

    async def frames() -> AsyncIterator[str]:
        def frame(event: str, data: dict[str, Any]) -> str:
            return f"event: {event}\ndata: {json.dumps(data)}\n\n"

        yield frame("chat_started", {"intent": resolution.intent.value})

        if resolution.intent.starts_a_run:
            yield frame("run_suggested", {"mode": resolution.mode.value})
            return

        answer = ConversationService(
            project_id=project_id, org_id=principal.org_id
        ).answer(resolution)
        if answer is not None:
            yield frame("token", {"text": answer.text})
            yield frame(
                "chat_finished",
                {"suggestions": answer.suggestions or [], "source": "deterministic"},
            )
            return

        context = _chat_context(project_id, principal)
        produced = False
        try:
            async for piece in _engine().answer_stream(payload.message, context=context):
                produced = True
                yield frame("token", {"text": piece})
        except Exception as exc:  # noqa: BLE001 - a chat reply must never fail the panel
            log.debug("chat stream failed", exc_info=True)
            if not produced:
                yield frame(
                    "token",
                    {
                        "text": (
                            "I could not reach a model in time to answer that. Ask me about a "
                            "run, a failure or today's cost and I will answer from my own "
                            "records, which needs no model at all."
                        )
                    },
                )
            yield frame("chat_finished", {"suggestions": [], "source": "error", "detail": str(exc)[:200]})
            return

        yield frame("chat_finished", {"suggestions": [], "source": "model"})

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Without this an intervening proxy will buffer the whole response
            # and deliver it at once, which is precisely what streaming is for
            # avoiding.
            "X-Accel-Buffering": "no",
        },
    )


def _chat_context(project_id: str, principal: Principal) -> str:
    """A few honest facts about this project, for answering questions about it."""
    if not project_id:
        return "No project is bound to this workspace yet."
    try:
        with session_scope() as session:
            project = session.get(ProjectRow, project_id)
            if project is None or (project.org_id and project.org_id != principal.org_id):
                return "No project is bound to this workspace yet."
            runs = list(
                session.execute(
                    select(RunRow)
                    .where(RunRow.project_id == project_id)
                    .order_by(RunRow.created_at.desc())
                    .limit(3)
                ).scalars()
            )
            lines = [
                f"Project: {project.name}",
                f"Repository: {project.repository_path}",
                f"Application under test: {project.base_url or '(none set)'}",
            ]
            if runs:
                lines.append("Recent runs:")
                lines += [
                    f"  - {r.instruction[:70]} ({r.status}, {r.tests_passed}/{r.tests_total} passing)"
                    for r in runs
                ]
    except Exception:  # noqa: BLE001
        return ""
    return "\n".join(lines)


@api.post("/runs", response_model=RunSummary, status_code=201, tags=["runs"])
async def create_run(payload: RunCreate, principal: Principal = requires("run:create")) -> RunSummary:
    if payload.auto_approve and not principal.can("approval:respond"):
        raise HTTPException(status_code=403, detail="auto_approve requires the approval:respond permission")

    with session_scope() as session:
        _owned_project(session, payload.project_id, principal)

    engine = _engine()
    run_id = engine.create_run(
        RunRequest(
            project_id=payload.project_id,
            instruction=payload.instruction,
            mode=payload.mode,
            target_url=payload.target_url,
            jira_issue=payload.jira_issue,
            tags=payload.tags,
            test_filter=payload.test_filter,
            max_cost_usd=payload.max_cost_usd,
            auto_approve=payload.auto_approve,
            metadata=payload.metadata,
        ),
        user_id=principal.user_id,
        org_id=principal.org_id,
    )
    if payload.start:
        await engine.start(run_id)

    with session_scope() as session:
        row = session.get(RunRow, run_id)
        project = session.get(ProjectRow, payload.project_id)
        return _run_summary(row, project.name if project else "")


@api.get("/runs", response_model=list[RunSummary], tags=["runs"])
async def list_runs(
    principal: Principal = requires("run:read"),
    project_id: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[RunSummary]:
    with session_scope() as session:
        stmt = select(RunRow).where(RunRow.org_id == principal.org_id)
        if project_id:
            stmt = stmt.where(RunRow.project_id == project_id)
        if status_filter:
            stmt = stmt.where(RunRow.status == status_filter)
        rows = list(
            session.execute(stmt.order_by(desc(RunRow.created_at)).limit(limit).offset(offset)).scalars()
        )
        names = {p.id: p.name for p in session.execute(select(ProjectRow)).scalars()}
        pending = {
            a.run_id: a.id
            for a in session.execute(
                select(ApprovalRow).where(ApprovalRow.status == ApprovalStatus.PENDING.value)
            ).scalars()
        }
        return [_run_summary(r, names.get(r.project_id, ""), pending.get(r.id, "")) for r in rows]


@api.get("/runs/{run_id}", response_model=RunDetail, tags=["runs"])
async def get_run(run_id: str, principal: Principal = requires("run:read")) -> RunDetail:
    with session_scope() as session:
        row = _owned_run(session, run_id, principal)
        project = session.get(ProjectRow, row.project_id)
        pending = session.execute(
            select(ApprovalRow).where(
                ApprovalRow.run_id == run_id, ApprovalRow.status == ApprovalStatus.PENDING.value
            )
        ).scalar_one_or_none()
        traces = [
            {
                "id": t.id, "agent": t.agent, "status": t.status, "sequence": t.sequence,
                "provider": t.provider, "model": t.model, "total_tokens": t.total_tokens,
                "cost_usd": round(t.cost_usd, 6), "latency_ms": t.latency_ms,
                "llm_calls": t.llm_calls, "tool_calls": t.tool_calls,
                "input_summary": t.input_summary, "output_summary": t.output_summary,
                "error": t.error, "started_at": _iso(t.started_at), "ended_at": _iso(t.ended_at),
            }
            for t in session.execute(
                select(AgentTraceRow).where(AgentTraceRow.run_id == run_id).order_by(AgentTraceRow.sequence)
            ).scalars()
        ]
        metadata = row.metadata_json or {}
        base = _run_summary(row, project.name if project else "", pending.id if pending else "")
        return RunDetail(
            **base.model_dump(),
            requirement=row.requirement, test_plan=row.test_plan, code_bundle=row.code_bundle,
            standards_report=row.standards_report, execution=row.execution, exploration=row.exploration,
            analyses=row.analyses or [], heals=row.heals or [], report=row.report,
            traces=traces, warnings=metadata.get("warnings", []), notes=metadata.get("notes", []),
            visited=metadata.get("visited", []),
        )


@api.get("/runs/{run_id}/events", tags=["runs"])
async def run_events(
    run_id: str,
    principal: Principal = requires("run:read"),
    since: str | None = None,
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, Any]:
    """Replay the persisted event log (what the WebSocket streamed live)."""
    with session_scope() as session:
        _owned_run(session, run_id, principal)
        stmt = select(RunEventRow).where(RunEventRow.run_id == run_id)
        if since:
            with contextlib.suppress(ValueError):
                stmt = stmt.where(RunEventRow.created_at > datetime.fromisoformat(since))
        rows = list(session.execute(stmt.order_by(RunEventRow.created_at).limit(limit)).scalars())
        return {
            "run_id": run_id,
            "count": len(rows),
            "events": [
                {
                    "id": e.id, "type": e.type, "agent": e.agent, "level": e.level,
                    "message": e.message, "data": e.data, "progress": e.progress,
                    "at": _iso(e.created_at),
                }
                for e in rows
            ],
        }


@api.get("/runs/{run_id}/trace", tags=["runs"])
async def run_trace(run_id: str, principal: Principal = requires("run:read")) -> dict[str, Any]:
    """Full execution trace: agents, LLM calls, tool calls. The audit view."""
    with session_scope() as session:
        _owned_run(session, run_id, principal)
        llm = list(session.execute(select(LLMCallRow).where(LLMCallRow.run_id == run_id)).scalars())
        tools_used = list(session.execute(select(ToolCallRow).where(ToolCallRow.run_id == run_id)).scalars())
        audits = list(
            session.execute(select(AuditRow).where(AuditRow.run_id == run_id).order_by(AuditRow.created_at)).scalars()
        )
        return {
            "run_id": run_id,
            "llm_calls": [
                {
                    "id": c.id, "agent": c.agent, "provider": c.provider, "model": c.model,
                    "capability": c.capability, "prompt_tokens": c.prompt_tokens,
                    "completion_tokens": c.completion_tokens, "total_tokens": c.total_tokens,
                    "cost_usd": round(c.cost_usd, 8), "latency_ms": c.latency_ms,
                    "status": c.status, "error": c.error, "fallback_from": c.fallback_from,
                    "prompt_preview": c.prompt_preview, "at": _iso(c.created_at),
                }
                for c in llm
            ],
            "tool_calls": [
                {
                    "id": t.id, "agent": t.agent, "category": t.category, "tool": t.tool,
                    "status": t.status, "latency_ms": t.latency_ms, "arguments": t.arguments_preview,
                    "result": t.result_preview, "error": t.error, "at": _iso(t.created_at),
                }
                for t in tools_used
            ],
            "audit": [
                {
                    "action": a.action, "resource": a.resource, "outcome": a.outcome,
                    "detail": a.detail, "user_id": a.user_id, "at": _iso(a.created_at),
                }
                for a in audits
            ],
        }


@api.get("/runs/{run_id}/diff", response_class=PlainTextResponse, tags=["runs"])
async def run_diff(run_id: str, principal: Principal = requires("run:read")) -> str:
    """The unified diff of everything this run proposes — what the human reviews."""
    with session_scope() as session:
        row = _owned_run(session, run_id, principal)
        bundle = row.code_bundle or {}
    parts: list[str] = []
    for change in bundle.get("changes", []):
        parts.append(change.get("diff") or f"--- new file {change.get('path')} ---\n{change.get('content', '')}")
    return "\n".join(parts) or "(no changes proposed)"


@api.get("/runs/{run_id}/report", tags=["runs"])
async def run_report(
    run_id: str, principal: Principal = requires("report:read"), format: str = Query(default="json")
) -> Any:
    with session_scope() as session:
        row = _owned_run(session, run_id, principal)
        report = row.report or {}
    if not report:
        raise HTTPException(status_code=404, detail="this run has no report yet")
    if format == "markdown":
        return PlainTextResponse(report.get("markdown", ""), media_type="text/markdown")
    if format == "html":
        return HTMLResponse(report.get("html", ""))
    return report


@api.get("/runs/{run_id}/artifacts", tags=["runs"])
async def run_artifacts(run_id: str, principal: Principal = requires("run:read")) -> dict[str, Any]:
    with session_scope() as session:
        _owned_run(session, run_id, principal)
        rows = list(session.execute(select(ArtifactRow).where(ArtifactRow.run_id == run_id)).scalars())
        return {
            "run_id": run_id,
            "artifacts": [
                {"id": a.id, "kind": a.kind, "name": a.name, "content_type": a.content_type,
                 "size_bytes": a.size_bytes, "exists": Path(a.path).exists()}
                for a in rows
            ],
        }


@api.get("/runs/{run_id}/artifacts/{artifact_id}", tags=["runs"])
async def download_artifact(
    run_id: str, artifact_id: str, principal: Principal = requires("run:read")
) -> FileResponse:
    with session_scope() as session:
        _owned_run(session, run_id, principal)
        row = session.get(ArtifactRow, artifact_id)
        if row is None or row.run_id != run_id:
            raise HTTPException(status_code=404, detail="artifact not found")
        path = Path(row.path)
        if not path.exists():
            raise HTTPException(status_code=410, detail="artifact file is no longer on disk")
        return FileResponse(path, media_type=row.content_type, filename=row.name)


@api.post("/runs/{run_id}/resume", response_model=RunSummary, tags=["runs"])
async def resume_run(run_id: str, principal: Principal = requires("run:create")) -> RunSummary:
    """Continue a run that is queued after an approval (or was interrupted)."""
    engine = _engine()
    with session_scope() as session:
        row = _owned_run(session, run_id, principal)
        if row.status in (RunStatus.RUNNING.value, RunStatus.WAITING_APPROVAL.value):
            raise HTTPException(status_code=409, detail=f"run is {row.status}; nothing to resume")
    await engine.start(run_id)
    with session_scope() as session:
        row = session.get(RunRow, run_id)
        return _run_summary(row)


@api.post("/runs/{run_id}/cancel", status_code=202, tags=["runs"])
async def cancel_run(run_id: str, principal: Principal = requires("run:cancel")) -> dict[str, str]:
    with session_scope() as session:
        _owned_run(session, run_id, principal)
    _engine().cancel(run_id, user_id=principal.user_id)
    return {"run_id": run_id, "status": RunStatus.CANCELLED.value}


def _owned_run(session: Any, run_id: str, principal: Principal) -> RunRow:
    row = session.get(RunRow, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    if row.org_id and row.org_id != principal.org_id and not principal.can("*"):
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return row


# =========================================================================== #
# Approvals — the human-in-the-loop surface
# =========================================================================== #
@api.get("/approvals", response_model=list[ApprovalOut], tags=["approvals"])
async def list_approvals(
    principal: Principal = requires("run:read"),
    run_id: str | None = None,
    project_id: str | None = None,
    include_resolved: bool = False,
) -> list[ApprovalOut]:
    with session_scope() as session:
        stmt = select(ApprovalRow)
        if not include_resolved:
            stmt = stmt.where(ApprovalRow.status == ApprovalStatus.PENDING.value)
        if run_id:
            stmt = stmt.where(ApprovalRow.run_id == run_id)
        if project_id:
            stmt = stmt.where(ApprovalRow.project_id == project_id)
        rows = list(session.execute(stmt.order_by(desc(ApprovalRow.created_at)).limit(200)).scalars())
        return [
            ApprovalOut(
                id=r.id, run_id=r.run_id, project_id=r.project_id, kind=r.kind, title=r.title,
                description=r.description, risk=r.risk, payload=r.payload,
                diff_preview=r.diff_preview, status=r.status, created_at=_iso(r.created_at),
            )
            for r in rows
        ]


@api.post("/approvals/{approval_id}", tags=["approvals"])
async def respond_approval(
    approval_id: str, decision: ApprovalDecision, principal: Principal = requires("approval:respond")
) -> dict[str, Any]:
    """Record the decision and, when approved, continue the run automatically."""
    engine = _engine()
    run_id = engine.respond_to_approval(
        approval_id,
        approved=decision.approved,
        user_id=principal.user_id,
        comment=decision.comment,
        changes_requested=decision.changes_requested,
    )
    if decision.approved:
        await engine.start(run_id)
    return {
        "approval_id": approval_id,
        "run_id": run_id,
        "approved": decision.approved,
        "resumed": decision.approved,
    }


# =========================================================================== #
# Metrics & governance
# =========================================================================== #
@api.get("/metrics", tags=["metrics"])
async def metrics(
    principal: Principal = requires("report:read"),
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope() as session:
        runs = list(
            session.execute(
                select(RunRow).where(RunRow.org_id == principal.org_id, RunRow.created_at >= since)
            ).scalars()
        )
        by_status: dict[str, int] = {}
        for run in runs:
            by_status[run.status] = by_status.get(run.status, 0) + 1

        cost_rows = list(
            session.execute(
                select(CostDailyRow).where(CostDailyRow.org_id == principal.org_id, CostDailyRow.day >= since.strftime("%Y-%m-%d"))
            ).scalars()
        )
        by_day: dict[str, float] = {}
        by_model: dict[str, float] = {}
        by_provider: dict[str, float] = {}
        for row in cost_rows:
            by_day[row.day] = round(by_day.get(row.day, 0.0) + row.cost_usd, 6)
            by_model[row.model] = round(by_model.get(row.model, 0.0) + row.cost_usd, 6)
            by_provider[row.provider] = round(by_provider.get(row.provider, 0.0) + row.cost_usd, 6)

        agent_stats = session.execute(
            select(
                AgentTraceRow.agent,
                func.count().label("runs"),
                func.sum(AgentTraceRow.total_tokens).label("tokens"),
                func.sum(AgentTraceRow.cost_usd).label("cost"),
                func.avg(AgentTraceRow.latency_ms).label("latency"),
                func.sum(case((AgentTraceRow.status == "failed", 1), else_=0)).label("failures"),
            ).group_by(AgentTraceRow.agent)
        ).all()

        heals = list(session.execute(select(HealHistoryRow)).scalars())
        flaky = list(
            session.execute(
                select(FlakyTestRow).where(FlakyTestRow.flakes > 0).order_by(desc(FlakyTestRow.flakes)).limit(25)
            ).scalars()
        )

    total_tests = sum(r.tests_total for r in runs)
    total_passed = sum(r.tests_passed for r in runs)
    return {
        "window_days": days,
        "runs": {
            "total": len(runs),
            "by_status": by_status,
            "succeeded": by_status.get(RunStatus.SUCCEEDED.value, 0),
            "avg_duration_s": round(sum(r.duration_s for r in runs) / len(runs), 2) if runs else 0.0,
        },
        "tests": {
            "total": total_tests,
            "passed": total_passed,
            "failed": sum(r.tests_failed for r in runs),
            "pass_rate_pct": round(100 * total_passed / total_tests, 2) if total_tests else 0.0,
            "scenarios_generated": sum(
                sum(len(f.get("scenarios", [])) for f in (r.test_plan or {}).get("features", [])) for r in runs
            ),
            "files_generated": sum(r.files_changed for r in runs),
        },
        "cost": {
            "total_usd": round(sum(r.total_cost_usd for r in runs), 6),
            "total_tokens": sum(r.total_tokens for r in runs),
            "llm_calls": sum(r.llm_calls for r in runs),
            "avg_per_run_usd": round(sum(r.total_cost_usd for r in runs) / len(runs), 6) if runs else 0.0,
            "by_day": dict(sorted(by_day.items())),
            "by_model": by_model,
            "by_provider": by_provider,
            "governance": CostGovernor(principal.org_id).snapshot(),
        },
        "agents": [
            {
                "agent": agent,
                "invocations": int(count or 0),
                "tokens": int(tokens or 0),
                "cost_usd": round(float(cost or 0.0), 6),
                "avg_latency_ms": int(latency or 0),
                "failures": int(failures or 0),
            }
            for agent, count, tokens, cost, latency, failures in agent_stats
        ],
        "self_healing": {
            "proposed": len(heals),
            "applied": sum(1 for h in heals if h.applied),
            "verified": sum(1 for h in heals if h.verified),
            "reverted": sum(1 for h in heals if h.reverted),
            "success_rate_pct": round(100 * sum(1 for h in heals if h.verified) / len(heals), 1) if heals else 0.0,
            "by_strategy": _count_by(heals, "strategy"),
        },
        "flaky_tests": [
            {
                "test_id": f.test_id, "name": f.test_name, "file": f.file_path,
                "runs": f.runs, "failures": f.failures, "flakes": f.flakes,
                "flake_rate_pct": round(100 * f.flakes / f.runs, 1) if f.runs else 0.0,
                "quarantined": f.quarantined,
            }
            for f in flaky
        ],
    }


def _count_by(rows: list[Any], attribute: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        key = str(getattr(row, attribute, "") or "unknown")
        out[key] = out.get(key, 0) + 1
    return out


@api.get("/audit", tags=["metrics"])
async def audit_log(
    principal: Principal = requires("report:read"),
    limit: int = Query(default=200, ge=1, le=2000),
    action: str | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    with session_scope() as session:
        stmt = select(AuditRow).where(AuditRow.org_id == principal.org_id)
        if action:
            stmt = stmt.where(AuditRow.action == action)
        if outcome:
            stmt = stmt.where(AuditRow.outcome == outcome)
        rows = list(session.execute(stmt.order_by(desc(AuditRow.created_at)).limit(limit)).scalars())
        return {
            "count": len(rows),
            "entries": [
                {
                    "id": r.id, "action": r.action, "resource": r.resource, "outcome": r.outcome,
                    "detail": r.detail, "user_id": r.user_id, "project_id": r.project_id,
                    "run_id": r.run_id, "at": _iso(r.created_at),
                }
                for r in rows
            ],
        }


# =========================================================================== #
# API keys
# =========================================================================== #
@api.post("/keys", status_code=201, tags=["admin"])
async def issue_key(payload: ApiKeyCreate, principal: Principal = requires("*")) -> dict[str, str]:
    result = create_api_key(
        principal.org_id, payload.user_id or principal.user_id, payload.name, payload.role
    )
    result["warning"] = "Store this key now — it is not recoverable."
    return result


@api.get("/keys", tags=["admin"])
async def list_keys(principal: Principal = requires("*")) -> dict[str, Any]:
    with session_scope() as session:
        rows = list(session.execute(select(ApiKeyRow).where(ApiKeyRow.org_id == principal.org_id)).scalars())
        return {
            "keys": [
                {
                    "id": r.id, "name": r.name, "prefix": r.key_prefix, "role": r.role,
                    "active": r.active, "last_used_at": _iso(r.last_used_at),
                    "created_at": _iso(r.created_at),
                }
                for r in rows
            ]
        }


@api.delete("/keys/{key_id}", status_code=204, tags=["admin"])
async def delete_key(key_id: str, principal: Principal = requires("*")) -> None:
    if not revoke_api_key(key_id):
        raise HTTPException(status_code=404, detail="key not found")


@api.get("/users", tags=["admin"])
async def list_users(principal: Principal = requires("*")) -> dict[str, Any]:
    with session_scope() as session:
        rows = list(session.execute(select(UserRow).where(UserRow.org_id == principal.org_id)).scalars())
        return {
            "users": [
                {"id": r.id, "email": r.email, "name": r.display_name, "role": r.role, "active": r.active}
                for r in rows
            ]
        }


@api.get("/metrics/cost", tags=["metrics"])
async def cost_metrics(
    principal: Principal = requires("report:read"),
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    """Cost dashboard: where the money went, per model / agent / tier / scenario."""
    from services.observability.metrics import cost_dashboard

    return cost_dashboard(org_id=principal.org_id, days=days)


@api.get("/metrics/management", tags=["metrics"])
async def management_metrics(
    principal: Principal = requires("report:read"),
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    """Management dashboard: coverage, pass rate, healing accuracy, intervention."""
    from services.observability.metrics import management_dashboard

    return management_dashboard(org_id=principal.org_id, days=days)


@api.get("/metrics/savings", tags=["metrics"])
async def savings_metrics(
    principal: Principal = requires("report:read"),
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    """What the knowledge layer actually avoided. Measured, not projected."""
    from services.observability.metrics import savings_report

    return savings_report(org_id=principal.org_id, days=days)


@api.get("/permissions", tags=["system"])
async def agent_permissions(_p: CurrentPrincipal) -> dict[str, Any]:
    """The least-privilege matrix each agent runs under."""
    from packages.agent_protocol import describe_permissions

    return {"agents": describe_permissions()}


@api.get("/graph", tags=["system"])
async def orchestrator_graph(_p: CurrentPrincipal) -> dict[str, Any]:
    """The agent pipeline, rendered from the compiled graph itself."""
    from agents.orchestrator.langgraph_graph import LANGGRAPH_AVAILABLE, build_orchestrator

    orchestrator = build_orchestrator()
    mermaid = orchestrator.mermaid() if hasattr(orchestrator, "mermaid") else ""
    return {
        "backend": type(orchestrator).__name__,
        "langgraph_available": LANGGRAPH_AVAILABLE,
        "mermaid": mermaid,
        "nodes": list(orchestrator.nodes),
    }


@api.get("/projects/{project_id}/knowledge", tags=["projects"])
async def project_knowledge(project_id: str, principal: Principal = requires("project:read")) -> dict[str, Any]:
    """What the platform knows about this project - the thing that makes it cheap."""
    from services.knowledge_service.application_map import ApplicationMap
    from services.knowledge_service.repository_map import RepositoryMap
    from services.knowledge_service.test_knowledge import QAKnowledgeGraph, TestKnowledgeStore

    with session_scope() as session:
        row = _owned_project(session, project_id, principal)
        repo_path = row.repository_path
        name = row.name

    repo_map = RepositoryMap.load(repo_path)
    app_map = ApplicationMap.load(repo_path)
    store = TestKnowledgeStore(project_id)
    graph = QAKnowledgeGraph(project_id)

    return {
        "project": name,
        "repository_map": repo_map.stats() if repo_map else None,
        "application_map": app_map.stats() if app_map else None,
        "test_knowledge": store.stats(),
        "knowledge_graph": graph.coverage(),
        "components": list((app_map.components or {}).keys()) if app_map else [],
        "known_routes": app_map.known_routes() if app_map else [],
    }


@api.get("/projects/{project_id}/coverage", tags=["projects"])
async def project_coverage(
    project_id: str,
    severity: str = Query(default="", description="Filter: high | medium | low"),
    principal: Principal = requires("project:read"),
) -> dict[str, Any]:
    """What the suite does NOT cover, and what to run to close each gap.

    A pure join over the application map, the test knowledge store and the QA
    graph, so it costs nothing to call and can run on every commit.
    """
    from services.knowledge_service.application_map import ApplicationMap
    from services.knowledge_service.coverage import analyse_coverage
    from services.knowledge_service.test_knowledge import QAKnowledgeGraph, TestKnowledgeStore

    with session_scope() as session:
        row = _owned_project(session, project_id, principal)
        repo_path = row.repository_path

    report = analyse_coverage(
        ApplicationMap.load(repo_path),
        TestKnowledgeStore(project_id),
        QAKnowledgeGraph(project_id),
    )
    payload = report.to_dict()
    if severity:
        payload["gaps"] = [g for g in payload["gaps"] if g["severity"] == severity]
        payload["gap_count"] = len(payload["gaps"])
    payload["summary"] = report.summary()
    return payload


@api.get("/projects/{project_id}/health", tags=["projects"])
async def project_suite_health(
    project_id: str, principal: Principal = requires("project:read")
) -> dict[str, Any]:
    """Per-test health, and which tests are costing more than they prove."""
    from services.execution_service.suite_health import suite_health

    with session_scope() as session:
        _owned_project(session, project_id, principal)
    return suite_health(project_id).to_dict()


@api.post("/projects/{project_id}/quarantine", tags=["projects"])
async def project_quarantine(
    project_id: str,
    body: dict[str, Any],
    principal: Principal = requires("project:write"),
) -> dict[str, Any]:
    """Quarantine or release a test, or apply the recommendations in bulk.

    A consistently failing test is refused unless `force` is set: that test is
    reporting something, and hiding it is how a defect ships green.
    """
    from services.execution_service.suite_health import auto_quarantine, set_quarantine

    with session_scope() as session:
        _owned_project(session, project_id, principal)

    test_id = str(body.get("test_id") or "").strip()
    if not test_id:
        return auto_quarantine(project_id, apply=bool(body.get("apply", False)))
    return set_quarantine(
        project_id,
        test_id,
        quarantined=bool(body.get("quarantined", True)),
        force=bool(body.get("force", False)),
    )


@api.post("/projects/{project_id}/batch/plan", tags=["projects"])
async def project_batch_plan(
    project_id: str,
    body: dict[str, Any],
    principal: Principal = requires("project:read"),
) -> dict[str, Any]:
    """Price a batch before anyone commits to it.

    Planning is a read: it starts nothing. Execution is driven by the CLI or the
    extension, which can stream progress — an HTTP request held open for the
    length of thirty runs would time out long before it finished.
    """
    from services.agent_engine.batch import plan_batch

    with session_scope() as session:
        _owned_project(session, project_id, principal)

    plan = plan_batch(
        project_id,
        list(body.get("requirements") or []),
        mode=str(body.get("mode") or "full"),
        max_cost_usd=float(body.get("max_cost_usd") or 0.0),
        max_items=int(body.get("max_items") or 50),
    )
    return plan.to_dict()


@api.post("/projects/{project_id}/explore", tags=["projects"])
async def project_explore(
    project_id: str,
    body: dict[str, Any] | None = None,
    principal: Principal = requires("project:write"),
) -> dict[str, Any]:
    """Probe the application for self-evident defects, with no requirement.

    Needs `project:write` rather than read: it sends requests to the running
    application, including empty form submissions.
    """
    from services.execution_service.exploratory import explore, regression_instruction
    from services.knowledge_service.application_map import ApplicationMap

    with session_scope() as session:
        row = _owned_project(session, project_id, principal)
        repo_path, base_url = row.repository_path, row.base_url

    app_map = ApplicationMap.load(repo_path)
    if app_map is None or not base_url:
        return {
            "findings": [],
            "summary": "Nothing to probe: this project has not been explored, or has no base URL.",
        }

    report = explore(app_map, base_url, max_probes=int((body or {}).get("max_probes", 40)))
    payload = report.to_dict()
    payload["next_steps"] = [
        {"finding": f.title, "instruction": regression_instruction(f)} for f in report.confirmed[:10]
    ]
    return payload


app.include_router(api)


# =========================================================================== #
# WebSocket: live run stream
# =========================================================================== #
@app.websocket("/ws/runs/{run_id}")
async def run_stream(websocket: WebSocket, run_id: str, api_key: str = Query(default="")) -> None:
    """Live event stream for one run.

    Browsers cannot set headers on a WebSocket handshake, so the key arrives as a
    query parameter here. It is validated exactly as an HTTP request would be.
    """
    from services.api_gateway.auth import hash_key

    with session_scope() as session:
        key_row = session.execute(
            select(ApiKeyRow).where(ApiKeyRow.key_hash == hash_key(api_key))
        ).scalar_one_or_none()
        if key_row is None or not key_row.active:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid API key")
            return
        run = session.get(RunRow, run_id)
        if run is None or (run.org_id and run.org_id != key_row.org_id):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="run not found")
            return
        replay = list(
            session.execute(
                select(RunEventRow).where(RunEventRow.run_id == run_id).order_by(RunEventRow.created_at).limit(500)
            ).scalars()
        )
        current_status = run.status

    await websocket.accept()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)

    def sink(event: Any) -> None:
        payload = {
            "id": event.id, "type": event.type,
            "agent": event.agent.value if event.agent else "",
            "level": event.level.value, "message": event.message,
            "data": event.data, "progress": event.progress,
            "at": event.at.isoformat(),
        }
        # The tracker runs on the event loop thread, but be safe either way.
        try:
            loop.call_soon_threadsafe(queue.put_nowait, payload)
        except RuntimeError:  # pragma: no cover - loop closing
            pass

    unsubscribe = _engine().subscribe(run_id, sink)
    try:
        # Replay history first so a late subscriber sees the whole run.
        await websocket.send_json(
            {
                "type": "__replay__",
                "run_id": run_id,
                "status": current_status,
                "events": [
                    {
                        "id": e.id, "type": e.type, "agent": e.agent, "level": e.level,
                        "message": e.message, "data": e.data, "progress": e.progress,
                        "at": _iso(e.created_at),
                    }
                    for e in replay
                ],
            }
        )

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20.0)
            except (TimeoutError, asyncio.TimeoutError):
                await websocket.send_json({"type": "__ping__"})
                with session_scope() as session:
                    row = session.get(RunRow, run_id)
                    if row is not None and RunStatus(row.status).terminal:
                        await websocket.send_json({"type": "__closed__", "status": row.status})
                        break
                continue
            await websocket.send_json(event)

    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.debug("websocket for run %s closed unexpectedly", run_id, exc_info=True)
    finally:
        unsubscribe()
        with contextlib.suppress(Exception):
            await websocket.close()


# =========================================================================== #
# Control plane UI
# =========================================================================== #
if (CONTROL_PLANE_DIR / "static").exists():
    app.mount("/static", StaticFiles(directory=str(CONTROL_PLANE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> str:
    index = CONTROL_PLANE_DIR / "templates" / "index.html"
    if index.exists():
        return index.read_text(encoding="utf-8")
    return (
        "<h1>QAgentic</h1>"
        "<p>Control plane is running. See <a href='/docs'>/docs</a> for the API.</p>"
    )
