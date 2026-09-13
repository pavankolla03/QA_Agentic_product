"""`aiqa` command line.

Everything the platform does is reachable without the extension, which matters
for two reasons: CI needs a headless entry point, and a QA engineer debugging a
run should not have to go through a UI to see what happened.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

app = typer.Typer(
    name="aiqa",
    help="AI QA Engineer — autonomous QA automation platform.",
    no_args_is_help=True,
    add_completion=False,
)
project_app = typer.Typer(help="Manage QA repositories.", no_args_is_help=True)
run_app = typer.Typer(help="Create and inspect runs.", no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(run_app, name="run")

# When output is piped (scripts, CI), Rich has no terminal width to measure and
# falls back to 80 columns — which silently truncates ids that users need to copy.
console = Console(width=None if sys.stdout.isatty() else 200)


def _bootstrap() -> None:
    from services.observability.db import init_db

    init_db()


# =========================================================================== #
@app.command()
def serve(
    host: str = typer.Option("", help="Bind address (defaults to AIQA_HOST)"),
    port: int = typer.Option(0, help="Port (defaults to AIQA_PORT)"),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (development)"),
) -> None:
    """Start the control plane API + dashboard."""
    import uvicorn

    from configs.settings import get_settings

    settings = get_settings()
    bind_host = host or settings.host
    bind_port = port or settings.port
    console.print(
        Panel.fit(
            f"[bold]AI QA Engineer[/bold]\n"
            f"API      http://{bind_host}:{bind_port}/api\n"
            f"Docs     http://{bind_host}:{bind_port}/docs\n"
            f"Dashboard http://{bind_host}:{bind_port}/\n"
            f"env={settings.env}  db={settings.database_url.split('://')[0]}  "
            f"providers={', '.join(settings.configured_providers)}",
            border_style="cyan",
        )
    )
    uvicorn.run(
        "services.api_gateway.app:app",
        host=bind_host, port=bind_port, reload=reload, log_level=settings.log_level.lower(),
    )


@app.command()
def init(
    force: bool = typer.Option(False, help="Recreate the schema, dropping existing data"),
) -> None:
    """Create the database schema and a bootstrap admin API key."""
    from services.api_gateway.auth import bootstrap_admin
    from services.observability.db import init_db

    if force and not typer.confirm("This will DROP all existing tables. Continue?"):
        raise typer.Abort()
    init_db(drop=force)
    info = bootstrap_admin()
    console.print("[green]database ready[/green]")
    table = Table(show_header=False, box=None)
    table.add_row("organization", info["org_id"])
    table.add_row("user", info["user_id"])
    table.add_row("api key", info["api_key"])
    console.print(table)
    if info["created"] == "true":
        console.print("[yellow]Change AIQA_BOOTSTRAP_API_KEY before exposing this instance.[/yellow]")


@app.command()
def doctor() -> None:
    """Check the environment: providers, database, toolchain."""
    _bootstrap()
    from configs.settings import get_settings
    from services.model_router.router import ModelRouter

    settings = get_settings()
    console.print(Panel.fit("[bold]Environment[/bold]", border_style="cyan"))
    table = Table("check", "result")
    table.add_row("env", settings.env)
    table.add_row("database", settings.database_url.split("://")[0])
    table.add_row("configured providers", ", ".join(settings.configured_providers))
    table.add_row("daily cost limit", f"${settings.daily_cost_limit_usd:.2f}")
    table.add_row("per-run cost limit", f"${settings.per_run_cost_limit_usd:.2f}")
    console.print(table)

    async def _providers() -> dict:
        return await ModelRouter().status()

    snapshot = asyncio.run(_providers())
    provider_table = Table("provider", "configured", "healthy", "default model")
    for name, info in snapshot["providers"].items():
        provider_table.add_row(
            name,
            "yes" if info["configured"] else "no",
            "[green]yes[/green]" if info["healthy"] else "[red]no[/red]",
            info["default_model"] or "—",
        )
    console.print(provider_table)

    route_table = Table("capability", "resolved to")
    for capability, route in snapshot["active_routes"].items():
        route_table.add_row(
            capability,
            f"{route['provider']}/{route['model']}" + (" (free)" if route.get("free") else "") if route else "[red]none[/red]",
        )
    console.print(route_table)
    if all(
        (route or {}).get("provider") in ("mock", "hashing")
        for route in snapshot["active_routes"].values()
    ):
        console.print(
            "\n[yellow]No real LLM is reachable — the platform will run in deterministic offline mode.[/yellow]\n"
            "[dim]Start Ollama (`ollama serve && ollama pull qwen2.5-coder:7b`) or set OPENROUTER_API_KEY.[/dim]"
        )


# =========================================================================== #
@project_app.command("add")
def project_add(
    name: str = typer.Argument(..., help="Project name"),
    path: str = typer.Argument(..., help="Path to the QA automation repository"),
    base_url: str = typer.Option("", help="URL of the application under test"),
    api_base_url: str = typer.Option("", help="Base URL of the application's API"),
    db_ref: str = typer.Option("", help="NAME of an env var holding the database DSN"),
) -> None:
    """Register a QA repository."""
    _bootstrap()
    from packages.aiqa_types.models import new_id
    from services.api_gateway.auth import bootstrap_admin
    from services.observability.db import session_scope
    from services.observability.models import ProjectRow

    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        console.print(f"[red]not a directory:[/red] {root}")
        raise typer.Exit(1)
    if "://" in db_ref:
        console.print("[red]--db-ref must be an environment variable NAME, not a connection string[/red]")
        raise typer.Exit(1)

    info = bootstrap_admin()
    project_id = new_id("prj")
    with session_scope() as session:
        session.add(
            ProjectRow(
                id=project_id, org_id=info["org_id"], name=name, repository_path=str(root),
                base_url=base_url, api_base_url=api_base_url, database_dsn_ref=db_ref,
            )
        )
    console.print(f"[green]registered[/green] {name} -> {root}\n  project id: {project_id}")


@project_app.command("list")
def project_list() -> None:
    """List registered projects."""
    _bootstrap()
    from sqlalchemy import select

    from services.observability.db import session_scope
    from services.observability.models import ProjectRow

    with session_scope() as session:
        rows = list(session.execute(select(ProjectRow).order_by(ProjectRow.name)).scalars())
    if not rows:
        console.print("[dim]no projects registered — use `aiqa project add`[/dim]")
        return
    table = Table(box=None, pad_edge=False)
    table.add_column("id", no_wrap=True, overflow="fold")
    for column in ("name", "repository", "app url", "runner"):
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(row.id, row.name, row.repository_path, row.base_url or "—", row.framework)
    console.print(table)


@project_app.command("index")
def project_index(project_id: str = typer.Argument(..., help="Project id")) -> None:
    """Index a repository so agents learn its conventions."""
    _bootstrap()

    from services.knowledge_service.indexer import RepositoryIndexer
    from services.model_router.router import ModelRouter
    from services.observability.db import session_scope
    from services.observability.models import ProjectRow

    with session_scope() as session:
        row = session.get(ProjectRow, project_id)
        if row is None:
            console.print(f"[red]project {project_id} not found[/red]")
            raise typer.Exit(1)
        repo_path = row.repository_path

    async def _index():
        return await RepositoryIndexer(project_id, repo_path).index(router=ModelRouter())

    with console.status("indexing repository..."):
        profile = asyncio.run(_index())

    console.print(
        f"[green]indexed[/green] {profile.file_count} files, {len(profile.symbols)} symbols, "
        f"{profile.indexed_chunks} chunks"
    )
    console.print(Panel(profile.conventions_summary, title="learned conventions", border_style="dim"))


@project_app.command("lint")
def project_lint(project_id: str = typer.Argument(..., help="Project id")) -> None:
    """Audit an existing suite against the organization standards."""
    _bootstrap()
    from agents.base import AgentContext
    from agents.standards.agent import lint_existing_repo
    from configs.settings import load_project_standards
    from packages.aiqa_types.models import Project
    from services.knowledge_service.indexer import RepositoryIndexer
    from services.observability.db import session_scope
    from services.observability.models import ProjectRow

    with session_scope() as session:
        row = session.get(ProjectRow, project_id)
        if row is None:
            console.print(f"[red]project {project_id} not found[/red]")
            raise typer.Exit(1)
        project = Project(
            id=row.id, org_id=row.org_id, name=row.name,
            repository_path=row.repository_path, language=row.language,
        )

    profile, _ = RepositoryIndexer(project_id, project.repository_path).scan()
    ctx = AgentContext(run_id="", project=project, instruction="lint",
                       standards=load_project_standards(project.repository_path))
    ctx.repo_profile = profile
    report = lint_existing_repo(ctx)

    console.print(
        f"checked {report.files_checked} file(s) against {report.rules_applied} rule(s): "
        f"[red]{report.error_count} error(s)[/red], [yellow]{report.warning_count} warning(s)[/yellow]"
    )
    if report.violations:
        table = Table("rule", "severity", "file:line", "message")
        for violation in report.violations[:60]:
            table.add_row(
                violation.rule_id, violation.severity.value,
                f"{violation.file_path}:{violation.line}", violation.message[:70],
            )
        console.print(table)
    raise typer.Exit(0 if report.passed else 1)


# =========================================================================== #
@run_app.command("start")
def run_start(
    project_id: str = typer.Argument(..., help="Project id"),
    instruction: str = typer.Argument(..., help='What to automate, e.g. "Automate resident registration"'),
    mode: str = typer.Option("full", help="plan_only | generate | full | autonomous | execute_only | heal_only"),
    auto_approve: bool = typer.Option(False, "--yes", "-y", help="Approve every gate automatically (CI)"),
    offline: bool = typer.Option(False, help="Force the deterministic offline provider"),
    show_report: bool = typer.Option(True, help="Print the report when the run finishes"),
) -> None:
    """Create and drive a run to completion."""
    _bootstrap()

    from packages.aiqa_types.enums import RunMode
    from packages.aiqa_types.models import RunRequest
    from services.agent_engine.engine import AgentEngine
    from services.observability.db import session_scope
    from services.observability.models import RunRow

    try:
        run_mode = RunMode(mode)
    except ValueError:
        console.print(f"[red]unknown mode '{mode}'[/red]")
        raise typer.Exit(1) from None

    engine = AgentEngine(offline=offline or None)
    run_id = engine.create_run(
        RunRequest(project_id=project_id, instruction=instruction, mode=run_mode, auto_approve=auto_approve),
        user_id="cli",
    )
    console.print(f"run [cyan]{run_id}[/cyan] created ({run_mode.value})")

    def on_event(event) -> None:
        if event.type == "agent_started":
            console.print(f"  [dim]>[/dim] [bold]{event.agent.value if event.agent else ''}[/bold]")
        elif event.type == "log":
            console.print(f"    [dim]{event.message[:150]}[/dim]")
        elif event.type == "approval_required":
            console.print(f"  [yellow]! approval required:[/yellow] {event.message}")
        elif event.type == "agent_failed":
            console.print(f"  [red]x {event.message[:160]}[/red]")

    engine.subscribe(run_id, on_event)
    result = asyncio.run(engine.run_to_completion(run_id, auto_approve=auto_approve))

    console.print(f"\nstatus: [bold]{result.status.value}[/bold]")
    if result.status.value == "waiting_approval":
        approvals = engine.pending_approvals(run_id=run_id)
        for approval in approvals:
            console.print(
                Panel(
                    f"{approval['description'][:1500]}\n\n[dim]approve with:[/dim] "
                    f"aiqa run approve {approval['id']}",
                    title=f"[yellow]{approval['kind']}[/yellow] — {approval['title']}",
                    border_style="yellow",
                )
            )
    if result.error:
        console.print(f"[red]{result.error}[/red]")

    with session_scope() as session:
        row = session.get(RunRow, run_id)
        if row and show_report and row.report:
            console.print(Panel(row.report.get("markdown", "")[:6000], title="report", border_style="cyan"))
        if row:
            console.print(
                f"cost: ${row.total_cost_usd:.6f} · {row.total_tokens:,} tokens · "
                f"{row.llm_calls} LLM call(s) · {row.duration_s:.1f}s"
            )
    raise typer.Exit(0 if result.status.value in ("succeeded", "waiting_approval") else 1)


@run_app.command("approve")
def run_approve(
    approval_id: str = typer.Argument(..., help="Approval id"),
    reject: bool = typer.Option(False, help="Reject instead of approve"),
    comment: str = typer.Option("", help="Reviewer comment"),
    resume: bool = typer.Option(True, help="Continue the run after approving"),
) -> None:
    """Respond to a pending approval."""
    _bootstrap()
    from services.agent_engine.engine import AgentEngine

    engine = AgentEngine()
    run_id = engine.respond_to_approval(approval_id, approved=not reject, user_id="cli", comment=comment)
    console.print(f"{'rejected' if reject else 'approved'} — run [cyan]{run_id}[/cyan]")
    if resume and not reject:
        result = asyncio.run(engine.run_to_completion(run_id))
        console.print(f"status: [bold]{result.status.value}[/bold]")


@run_app.command("list")
def run_list(limit: int = typer.Option(20, help="How many runs to show")) -> None:
    """List recent runs."""
    _bootstrap()
    from sqlalchemy import desc, select

    from services.observability.db import session_scope
    from services.observability.models import RunRow

    with session_scope() as session:
        rows = list(session.execute(select(RunRow).order_by(desc(RunRow.created_at)).limit(limit)).scalars())
    if not rows:
        console.print("[dim]no runs yet[/dim]")
        return
    table = Table(box=None, pad_edge=False)
    table.add_column("id", no_wrap=True, overflow="fold")
    for column in ("status", "instruction", "tests", "cost", "when"):
        table.add_column(column, overflow="fold")
    for row in rows:
        tests = f"{row.tests_passed}/{row.tests_total}" if row.tests_total else "—"
        table.add_row(
            row.id, row.status, row.instruction[:44], tests,
            f"${row.total_cost_usd:.4f}", row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
        )
    console.print(table)


@run_app.command("show")
def run_show(
    run_id: str = typer.Argument(..., help="Run id"),
    trace: bool = typer.Option(False, help="Include the per-agent trace"),
    diff: bool = typer.Option(False, help="Show the proposed diff"),
) -> None:
    """Inspect one run."""
    _bootstrap()
    from sqlalchemy import select

    from services.observability.db import session_scope
    from services.observability.models import AgentTraceRow, RunRow

    with session_scope() as session:
        row = session.get(RunRow, run_id)
        if row is None:
            console.print(f"[red]run {run_id} not found[/red]")
            raise typer.Exit(1)
        traces = list(
            session.execute(
                select(AgentTraceRow).where(AgentTraceRow.run_id == run_id).order_by(AgentTraceRow.sequence)
            ).scalars()
        )
        report = row.report or {}
        bundle = row.code_bundle or {}
        metadata = row.metadata_json or {}

    console.print(
        Panel.fit(
            f"[bold]{row.instruction}[/bold]\n"
            f"status={row.status}  mode={row.mode}  iteration={row.iteration}\n"
            f"tests={row.tests_passed}/{row.tests_total}  files={row.files_changed}\n"
            f"cost=${row.total_cost_usd:.6f}  tokens={row.total_tokens:,}  calls={row.llm_calls}\n"
            f"path: {' -> '.join(metadata.get('visited', []))}",
            title=run_id, border_style="cyan",
        )
    )
    if trace and traces:
        table = Table("#", "agent", "status", "model", "tokens", "cost", "ms", "output")
        for t in traces:
            table.add_row(
                str(t.sequence), t.agent, t.status, t.model or "—", f"{t.total_tokens:,}",
                f"${t.cost_usd:.6f}", str(t.latency_ms), (t.output_summary or t.error)[:40],
            )
        console.print(table)
    if diff:
        for change in bundle.get("changes", []):
            console.print(Panel(change.get("diff") or change.get("content", "")[:3000],
                                title=change.get("path", ""), border_style="dim"))
    if report.get("markdown"):
        console.print(Panel(report["markdown"][:8000], title="report", border_style="green"))


@run_app.command("approvals")
def run_approvals() -> None:
    """List everything waiting on a human."""
    _bootstrap()
    from services.agent_engine.engine import AgentEngine

    pending = AgentEngine().pending_approvals()
    if not pending:
        console.print("[dim]nothing pending[/dim]")
        return
    for approval in pending:
        console.print(
            Panel(
                f"{approval['description'][:900]}\n\n[dim]aiqa run approve {approval['id']}[/dim]",
                title=f"[yellow]{approval['kind']}[/yellow] (risk={approval['risk']}) — {approval['title'][:70]}",
                border_style="yellow",
            )
        )


@app.command()
def selfcheck(
    live: bool = typer.Option(False, help="Use configured providers instead of offline mode"),
) -> None:
    """Run the end-to-end platform self-check."""
    from scripts.selfcheck import main as selfcheck_main

    raise typer.Exit(asyncio.run(selfcheck_main(live=live)))


@app.command()
def metrics(days: int = typer.Option(30, help="Window in days")) -> None:
    """Print platform metrics as JSON."""
    _bootstrap()
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from services.observability.db import session_scope
    from services.observability.models import CostDailyRow, RunRow
    from services.observability.tracker import CostGovernor

    since = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope() as session:
        runs = list(session.execute(select(RunRow).where(RunRow.created_at >= since)).scalars())
        costs = list(session.execute(select(CostDailyRow)).scalars())
    payload = {
        "runs": len(runs),
        "tests_total": sum(r.tests_total for r in runs),
        "tests_passed": sum(r.tests_passed for r in runs),
        "cost_usd": round(sum(r.total_cost_usd for r in runs), 6),
        "tokens": sum(r.total_tokens for r in runs),
        "by_model": {c.model: round(c.cost_usd, 6) for c in costs},
        "governance": CostGovernor().snapshot(),
    }
    console.print_json(json.dumps(payload))


if __name__ == "__main__":
    app()
