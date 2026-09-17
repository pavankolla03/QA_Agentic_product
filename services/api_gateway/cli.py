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
    help="QAgentic — autonomous QA automation platform.",
    no_args_is_help=True,
    add_completion=False,
)
project_app = typer.Typer(help="Manage QA repositories.", no_args_is_help=True)
run_app = typer.Typer(help="Create and inspect runs.", no_args_is_help=True)
standards_app = typer.Typer(help="Organization and project QA standards.", no_args_is_help=True)
knowledge_app = typer.Typer(help="Inspect what the platform knows.", no_args_is_help=True)
cost_app = typer.Typer(help="Cost and savings reporting.", no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(run_app, name="run")
app.add_typer(standards_app, name="standards")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(cost_app, name="cost")

# When output is piped (scripts, CI), Rich has no terminal width to measure and
# falls back to 80 columns — which silently truncates ids that users need to copy.
console = Console(width=None if sys.stdout.isatty() else 200)


def _bootstrap() -> None:
    from services.observability.db import init_db

    init_db()


# =========================================================================== #

def _already_serving(host: str, port: int) -> bool:
    """Is a healthy control plane already answering here?

    Deliberately asks `/api/health` rather than just probing the socket: a port
    held by something that is not us should still be reported, and the health
    endpoint is the only way to tell the difference in the message.
    """
    import urllib.error
    import urllib.request

    probe = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    try:
        with urllib.request.urlopen(f"http://{probe}:{port}/api/health", timeout=2) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False



@app.command()
def worker(
    concurrency: int = typer.Option(1, help="Runs this worker executes at once"),
) -> None:
    """Execute queued runs. Run one or more of these beside `serve`.

    Without a worker the API executes runs itself, which works and is the
    default — but a run then dies with the API process and competes with chat
    for the same event loop.
    """
    from configs.settings import get_settings

    settings = get_settings()
    url = getattr(settings, "redis_url", "")
    if not url:
        console.print(
            "[yellow]AIQA_REDIS_URL is not set.[/yellow]\n"
            "A worker reads from a queue; without one there is nothing to read.\n"
            "Either set it, or run only `aiqa serve` and the API will execute runs itself."
        )
        raise typer.Exit(code=1)

    from arq import run_worker

    from services.worker.main import WorkerSettings

    WorkerSettings.max_jobs = max(1, concurrency)
    console.print(
        Panel.fit(
            f"[bold]QAgentic worker[/bold]\n"
            f"queue         {url}\n"
            f"runs at once  {WorkerSettings.max_jobs}\n"
            f"db            {settings.database_url.split('://')[0]}",
            border_style="cyan",
        )
    )
    run_worker(WorkerSettings)  # type: ignore[arg-type]


@app.command()
def serve(
    host: str = typer.Option("", help="Bind address (defaults to AIQA_HOST)"),
    port: int = typer.Option(0, help="Port (defaults to AIQA_PORT)"),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (development)"),
    force: bool = typer.Option(False, help="Start even if something is already serving this port"),
) -> None:
    """Start the control plane API + dashboard."""
    import uvicorn

    from configs.settings import get_settings

    settings = get_settings()
    bind_host = host or settings.host
    bind_port = port or settings.port

    # Windows lets a second process bind a port that is already serving, and
    # then splits requests between them. The result is not an error anywhere —
    # it is a control plane that answers roughly half the time and hangs for
    # the rest, which from the extension is indistinguishable from a chat that
    # does not work. Two of these accumulated during one debugging session and
    # cost an hour.
    if not force and _already_serving(bind_host, bind_port):
        console.print(
            f"[yellow]Something is already serving http://{bind_host}:{bind_port}[/yellow]\n"
            "Nothing was started - that server is fine to use as it is.\n"
            "Pass --force to start anyway, or stop the other process first."
        )
        raise typer.Exit(code=0)
    console.print(
        Panel.fit(
            f"[bold]QAgentic[/bold]\n"
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


# =========================================================================== #
@standards_app.command("init")
def standards_init(
    path: str = typer.Argument(".", help="Repository to scaffold"),
    force: bool = typer.Option(False, help="Overwrite existing files"),
) -> None:
    """Create a starter `.aiqa/` with config, prose standards and an examples folder."""
    from services.knowledge_service.standards_engine import StandardsEngine

    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        console.print(f"[red]not a directory:[/red] {root}")
        raise typer.Exit(1)

    created = StandardsEngine(root).scaffold(force=force)
    if created:
        console.print(f"[green]created {len(created)} file(s)[/green] under {root / '.aiqa'}")
        for name in created:
            console.print(f"  {name}")
    else:
        console.print("[dim].aiqa already exists - nothing to do (use --force to overwrite)[/dim]")

    console.print(
        Panel.fit(
            "Next:\n"
            "  1. Edit .aiqa/standards/*.md in your own words - recognised phrasings become rules.\n"
            "  2. Copy a real Page Object, feature file and steps file into .aiqa/examples/.\n"
            "     The platform learns your naming, structure and locator style from them.\n"
            "  3. Run `aiqa standards show` to see what it resolved.",
            border_style="cyan",
        )
    )


@standards_app.command("show")
def standards_show(
    path: str = typer.Argument(".", help="Repository to inspect"),
    module: str = typer.Option("", help="Module-level overrides to apply"),
) -> None:
    """Show the merged standards actually in force, and where each part came from."""
    from services.knowledge_service.standards_engine import StandardsEngine

    root = Path(path).expanduser().resolve()
    resolved = StandardsEngine(root).resolve(module=module)

    console.print(Panel.fit("[bold]Sources (later wins)[/bold]", border_style="cyan"))
    for source in resolved.sources:
        console.print(f"  {source}")

    table = Table("rule", "severity", "applies to", "title")
    for rule in resolved.rules[:40]:
        applies = rule.get("applies_to")
        table.add_row(
            str(rule.get("id", "")),
            str(rule.get("severity", "")),
            ", ".join(applies) if isinstance(applies, list) else str(applies or "all"),
            str(rule.get("title", rule.get("message", "")))[:56],
        )
    console.print(table)

    if resolved.house_style.examples_analysed:
        console.print(Panel(resolved.house_style.briefing(), title="learned house style", border_style="dim"))
    else:
        console.print(
            "[yellow]No examples found.[/yellow] Drop real files into .aiqa/examples/ so the "
            "platform can learn your conventions instead of guessing."
        )

    if resolved.unparsed_prose:
        console.print("\n[yellow]Prose that could not be turned into rules:[/yellow]")
        for line in resolved.unparsed_prose:
            console.print(f"  - {line}")


@standards_app.command("parse")
def standards_parse(
    text: str = typer.Argument(..., help="A standards statement, in your own words"),
) -> None:
    """Check whether a prose standard is recognised, before you commit it."""
    from services.knowledge_service.standards_engine import parse_freeform_standards

    rules, unparsed = parse_freeform_standards(text)
    if rules:
        table = Table("rule", "severity", "check", "from")
        for rule in rules:
            table.add_row(
                str(rule.get("id")), str(rule.get("severity")),
                str(rule.get("check", rule.get("kind", "regex"))), str(rule.get("title", ""))[:50],
            )
        console.print(table)
    for line in unparsed:
        console.print(f"[yellow]not recognised:[/yellow] {line}")
        console.print("[dim]  It will be kept as prose context, but not enforced as a rule.[/dim]")


# =========================================================================== #
@knowledge_app.command("show")
def knowledge_show(project_id: str = typer.Argument(..., help="Project id")) -> None:
    """What the platform knows - the reason repeat runs are cheap."""
    _bootstrap()
    from services.knowledge_service.application_map import ApplicationMap
    from services.knowledge_service.repository_map import RepositoryMap
    from services.knowledge_service.test_knowledge import QAKnowledgeGraph, TestKnowledgeStore
    from services.observability.db import session_scope
    from services.observability.models import ProjectRow

    with session_scope() as session:
        row = session.get(ProjectRow, project_id)
        if row is None:
            console.print(f"[red]project {project_id} not found[/red]")
            raise typer.Exit(1)
        repo_path, name = row.repository_path, row.name

    repo_map = RepositoryMap.load(repo_path)
    app_map = ApplicationMap.load(repo_path)
    store = TestKnowledgeStore(project_id)
    graph = QAKnowledgeGraph(project_id)

    table = Table("layer", "state")
    table.add_row(
        "repository map",
        f"{repo_map.stats()['files']} files @ {repo_map.git_commit[:12]}" if repo_map else "[yellow]not indexed[/yellow]",
    )
    if app_map:
        stats = app_map.stats()
        table.add_row(
            "application map",
            f"{stats['pages']} routes, {stats['trusted_locators']}/{stats['locators']} trusted locators "
            f"(avg confidence {stats['avg_confidence']}), {stats['components']} components",
        )
    else:
        table.add_row("application map", "[yellow]not explored[/yellow]")
    table.add_row("test knowledge", f"{store.stats()['tests_known']} test(s) remembered")
    coverage = graph.coverage()
    table.add_row("knowledge graph", f"{coverage['nodes']} nodes, {coverage['coverage_pct']}% requirement coverage")
    console.print(Panel.fit(f"[bold]{name}[/bold]", border_style="cyan"))
    console.print(table)

    if app_map and app_map.known_routes():
        console.print("\n[bold]Routes known[/bold]")
        for route in app_map.known_routes():
            console.print(f"  {route}")
    if app_map and app_map.components:
        console.print("\n[bold]Reusable components[/bold]")
        for component, entry in app_map.components.items():
            scope = "shared" if entry.get("shared") else f"{len(entry.get('pages', []))} page"
            console.print(f"  {component} ({scope})")


@knowledge_app.command("coverage")
def coverage_gaps(
    project_id: str = typer.Argument(..., help="Project id"),
    severity: str = typer.Option("", help="Only show gaps at this severity: high | medium | low"),
    fail_on_high: bool = typer.Option(
        False, help="Exit non-zero when a high-severity gap exists (for CI)."
    ),
) -> None:
    """What the suite does not cover, worst first."""
    _bootstrap()
    from services.knowledge_service.application_map import ApplicationMap
    from services.knowledge_service.coverage import analyse_coverage
    from services.knowledge_service.test_knowledge import QAKnowledgeGraph, TestKnowledgeStore
    from services.observability.db import session_scope
    from services.observability.models import ProjectRow

    with session_scope() as session:
        row = session.get(ProjectRow, project_id)
        if row is None:
            console.print(f"[red]no such project:[/red] {project_id}")
            raise typer.Exit(1)
        repo_path = row.repository_path
        name = row.name

    report = analyse_coverage(
        ApplicationMap.load(repo_path),
        TestKnowledgeStore(project_id),
        QAKnowledgeGraph(project_id),
    )

    console.print(Panel.fit(f"[bold]Coverage — {name}[/bold]\n{report.summary()}", border_style="cyan"))

    table = Table(box=None, pad_edge=False)
    table.add_column("what", style="dim")
    table.add_column("covered", justify="right")
    table.add_column("total", justify="right")
    table.add_column("%", justify="right")
    table.add_row("routes", str(report.routes_covered), str(report.routes_total), f"{report.route_pct}%")
    table.add_row("endpoints", str(report.endpoints_covered), str(report.endpoints_total), f"{report.endpoint_pct}%")
    table.add_row("components", str(report.components_covered), str(report.components_total), "")
    table.add_row(
        "requirements", str(report.requirements_covered), str(report.requirements_total),
        f"{report.requirement_pct}%",
    )
    console.print(table)

    gaps = [g for g in report.gaps if not severity or g.severity == severity]
    if not gaps:
        console.print("\n[green]no gaps at this severity[/green]")
    else:
        console.print(f"\n[bold]{len(gaps)} gap(s)[/bold]  (worst first)")
        gap_table = Table(box=None, pad_edge=False)
        gap_table.add_column("sev", style="bold")
        gap_table.add_column("kind", style="dim")
        gap_table.add_column("what", no_wrap=True)
        gap_table.add_column("run this to close it", style="cyan")
        colours = {"high": "red", "medium": "yellow", "low": "dim"}
        for gap in gaps:
            gap_table.add_row(
                f"[{colours.get(gap.severity, 'white')}]{gap.severity}[/]",
                gap.kind,
                gap.label[:48],
                gap.suggested_instruction[:60],
            )
        console.print(gap_table)

    console.print(
        "\n[dim]A route counted as covered has a test touching it — that is not the "
        "same as being well tested.[/dim]"
    )
    if fail_on_high and report.by_severity("high"):
        raise typer.Exit(2)


@knowledge_app.command("reindex")
def knowledge_reindex(
    project_id: str = typer.Argument(..., help="Project id"),
    force: bool = typer.Option(False, help="Ignore caches and rebuild from scratch"),
) -> None:
    """Refresh the repository index. Incremental unless --force."""
    _bootstrap()
    from services.knowledge_service.incremental import IncrementalIndexer
    from services.model_router.router import ModelRouter
    from services.observability.db import session_scope
    from services.observability.models import ProjectRow

    with session_scope() as session:
        row = session.get(ProjectRow, project_id)
        if row is None:
            console.print(f"[red]project {project_id} not found[/red]")
            raise typer.Exit(1)
        repo_path = row.repository_path

    async def _sync():
        return await IncrementalIndexer(project_id, repo_path).sync(ModelRouter(), force=force)

    with console.status("syncing repository index..."):
        _profile, delta, repo_map = asyncio.run(_sync())
    console.print(f"[green]{delta.summary()}[/green]")
    console.print(f"  {repo_map.stats()}")


# =========================================================================== #
@cost_app.command("report")
def cost_report(days: int = typer.Option(30, help="Window in days")) -> None:
    """Cost dashboard: where the money went and what caching avoided."""
    _bootstrap()
    from services.observability.metrics import cost_dashboard, savings_report

    cost = cost_dashboard(days=days)
    savings = savings_report(days=days)
    totals, unit = cost["totals"], cost["unit_economics"]

    console.print(Panel.fit(f"[bold]Cost report - last {days} days[/bold]", border_style="cyan"))
    table = Table("metric", "value")
    table.add_row("runs", str(totals["runs"]))
    table.add_row("scenarios generated", str(totals["scenarios"]))
    table.add_row("LLM requests", str(totals["llm_requests"]))
    table.add_row("input / output tokens", f"{totals['input_tokens']:,} / {totals['output_tokens']:,}")
    table.add_row("free-model share", f"{totals['free_call_share_pct']}%")
    table.add_row("total cost", f"${totals['total_cost_usd']:.4f}")
    table.add_row("cost per scenario", f"${unit['cost_per_scenario_usd']:.5f}")
    table.add_row("requests per scenario", str(unit["requests_per_scenario"]))
    table.add_row("tokens per scenario", f"{unit['tokens_per_scenario']:,}")
    console.print(table)

    saved = Table("what caching avoided", "value")
    saved.add_row("repository index cache hits", f"{savings['repository_cache_hit_rate_pct']}%")
    saved.add_row("application map hits", f"{savings['application_map_hit_rate_pct']}%")
    saved.add_row("duplicate scenarios avoided", str(savings["duplicate_scenarios_avoided"]))
    saved.add_row("context tokens never sent", f"{savings['context_tokens_avoided']:,}")
    console.print(saved)

    if cost["by_model"]:
        models = Table("model", "calls", "tokens", "cost")
        for model, stats in list(cost["by_model"].items())[:8]:
            models.add_row(model, str(stats["calls"]), f"{stats['tokens']:,}", f"${stats['cost_usd']:.6f}")
        console.print(models)

    console.print(f"\n[dim]{cost['baseline_comparison']['note']}[/dim]")


@cost_app.command("management")
def cost_management(days: int = typer.Option(30, help="Window in days")) -> None:
    """Management dashboard: coverage, pass rate, healing accuracy, intervention."""
    _bootstrap()
    from services.observability.metrics import management_dashboard

    data = management_dashboard(days=days)
    for section in ("automation", "execution", "self_healing", "human_involvement"):
        table = Table(section.replace("_", " "), "value")
        for key, value in data[section].items():
            if key.endswith("note"):
                continue
            table.add_row(key.replace("_", " "), str(value))
        console.print(table)
    impact = data["estimated_impact"]
    console.print(
        Panel(
            f"Estimated hours saved: {impact['hours_saved_estimate']}\n\n{impact['assumption']}",
            title="impact (estimate)",
            border_style="dim",
        )
    )


@app.command()
def explore(
    project_id: str = typer.Argument(..., help="Project id"),
    max_probes: int = typer.Option(40, help="Cap on how many probes to run."),
    fail_on_finding: bool = typer.Option(False, help="Exit non-zero if anything confirmed is found."),
) -> None:
    """Look for self-evidently broken things, with no requirement to work from."""
    _bootstrap()
    from services.execution_service.exploratory import explore as run_exploration
    from services.execution_service.exploratory import regression_instruction
    from services.knowledge_service.application_map import ApplicationMap
    from services.observability.db import session_scope
    from services.observability.models import ProjectRow

    with session_scope() as session:
        row = session.get(ProjectRow, project_id)
        if row is None:
            console.print(f"[red]no such project:[/red] {project_id}")
            raise typer.Exit(1)
        repo_path, base_url, name = row.repository_path, row.base_url, row.name

    app_map = ApplicationMap.load(repo_path)
    if app_map is None:
        console.print(
            "[yellow]This project has never been explored, so there is nothing to probe.[/yellow]\n"
            "[dim]Run an automation first, or `aiqa run` with a base URL configured.[/dim]"
        )
        raise typer.Exit(0)
    if not base_url:
        console.print("[red]this project has no base_url configured[/red]")
        raise typer.Exit(1)

    report = run_exploration(app_map, base_url, max_probes=max_probes)
    console.print(
        Panel.fit(f"[bold]Exploratory pass — {name}[/bold]\n{report.summary()}", border_style="cyan")
    )

    if report.findings:
        colours = {"critical": "red", "high": "red", "medium": "yellow", "low": "dim", "observation": "dim"}
        table = Table(box=None, pad_edge=False)
        table.add_column("severity", style="bold")
        table.add_column("kind", style="dim")
        table.add_column("where", no_wrap=True)
        table.add_column("what")
        for finding in report.findings:
            marker = "" if finding.confidence == "confirmed" else " [dim](observation)[/dim]"
            table.add_row(
                f"[{colours.get(finding.severity, 'white')}]{finding.severity}[/]",
                finding.kind,
                finding.route[:28],
                finding.title[:60] + marker,
            )
        console.print(table)

        console.print("\n[bold]To turn a confirmed finding into a permanent test:[/bold]")
        for finding in report.confirmed[:5]:
            console.print(f'  aiqa run {project_id} "{regression_instruction(finding)}"')

    if report.unreachable:
        console.print(f"\n[dim]{len(report.unreachable)} probe(s) could not be reached[/dim]")

    console.print(
        "\n[dim]Findings are limited to failures that need no specification to recognise: "
        "crashes, error pages, dead links, absent validation. A clean pass is not a "
        "claim that the application is correct.[/dim]"
    )
    if fail_on_finding and report.confirmed:
        raise typer.Exit(2)


@app.command()
def batch(
    project_id: str = typer.Argument(..., help="Project id"),
    epic: str = typer.Option("", help="Jira epic key — automate every issue under it."),
    from_file: str = typer.Option("", help="Text file, one requirement per line."),
    mode: str = typer.Option("full", help="Run mode for every item."),
    max_cost_usd: float = typer.Option(5.0, help="Ceiling for the WHOLE batch, not per run."),
    max_items: int = typer.Option(25, help="Never queue more than this many."),
    auto_approve: bool = typer.Option(False, help="Approve every gate automatically."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Automate a whole epic instead of one ticket at a time.

    Prints the queue and its expected cost, then asks before spending anything.
    """
    _bootstrap()
    import asyncio
    from pathlib import Path as _Path

    from services.agent_engine.batch import execute_batch, plan_batch
    from services.agent_engine.engine import AgentEngine
    from services.observability.db import session_scope
    from services.observability.models import ProjectRow

    requirements: list[dict] = []
    if epic:
        from tools.base import ToolContext
        from tools.jira.jira_tools import JiraFetchEpicTool

        with session_scope() as session:
            row = session.get(ProjectRow, project_id)
            if row is None:
                console.print(f"[red]no such project:[/red] {project_id}")
                raise typer.Exit(1)
            repo_path = row.repository_path
        result = JiraFetchEpicTool().run(ToolContext(workspace_root=repo_path), epic_key=epic,
                                         max_issues=max_items)
        if not result.ok:
            console.print(f"[red]{result.error}[/red]")
            raise typer.Exit(1)
        requirements = result.data["issues"]
        console.print(f"[dim]{epic}: {len(requirements)} issue(s)[/dim]")
    elif from_file:
        path = _Path(from_file)
        if not path.exists():
            console.print(f"[red]no such file:[/red] {from_file}")
            raise typer.Exit(1)
        requirements = [
            {"key": f"L{i}", "instruction": line.strip()}
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if line.strip() and not line.strip().startswith("#")
        ]
    else:
        console.print("[red]give either --epic or --from-file[/red]")
        raise typer.Exit(1)

    plan = plan_batch(project_id, requirements, mode=mode,
                      max_cost_usd=max_cost_usd, max_items=max_items)
    if not plan.items:
        console.print("[yellow]nothing to run[/yellow]")
        raise typer.Exit(0)

    table = Table(box=None, pad_edge=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("key", no_wrap=True)
    table.add_column("priority")
    table.add_column("what")
    for index, item in enumerate(plan.items, start=1):
        table.add_row(str(index), item.key, item.priority, item.instruction.splitlines()[0][:64])
    console.print(table)

    console.print(
        f"\n[bold]{len(plan.items)} run(s)[/bold], estimated "
        f"[bold]${plan.estimated_cost_usd:.2f}[/bold]  [dim]({plan.estimate_basis})[/dim]"
    )
    console.print(f"batch ceiling: ${max_cost_usd:.2f}  ·  runs stop once it is reached")
    if not plan.within_budget:
        console.print(
            "[yellow]The estimate exceeds the ceiling. The batch will run in priority "
            "order and stop when the ceiling is hit.[/yellow]"
        )

    if not yes and not typer.confirm("\nStart this batch?", default=False):
        console.print("[dim]nothing started[/dim]")
        raise typer.Exit(0)

    engine = AgentEngine()
    result = asyncio.run(execute_batch(engine, plan, auto_approve=auto_approve))

    console.print(Panel.fit(f"[bold]{result.summary()}[/bold]", border_style="cyan"))
    outcome = Table(box=None, pad_edge=False)
    outcome.add_column("key", no_wrap=True)
    outcome.add_column("status")
    outcome.add_column("scenarios", justify="right")
    outcome.add_column("cost", justify="right")
    outcome.add_column("note")
    colours = {"succeeded": "green", "no_work": "yellow", "failed": "red", "skipped": "dim"}
    for item in [*result.completed, *result.failed, *result.skipped]:
        outcome.add_row(
            item.key,
            f"[{colours.get(item.status, 'white')}]{item.status}[/]",
            str(item.scenarios),
            f"${item.cost_usd:.4f}",
            (item.error or "")[:44],
        )
    console.print(outcome)
    if result.failed:
        raise typer.Exit(1)


@app.command("suite-health")
def suite_health_command(
    project_id: str = typer.Argument(..., help="Project id"),
    quarantine: bool = typer.Option(False, help="Apply the quarantine recommendations."),
    fail_on_broken: bool = typer.Option(False, help="Exit non-zero if a test never passes."),
) -> None:
    """How trustworthy the suite is, and which tests are eroding that."""
    _bootstrap()
    from services.execution_service.suite_health import auto_quarantine, suite_health

    report = suite_health(project_id)
    console.print(
        Panel.fit(
            f"[bold]Suite health — {report.health_score}%[/bold]\n{report.summary()}",
            border_style="cyan",
        )
    )
    if not report.tests:
        console.print("[dim]Run the suite at least once to build a history.[/dim]")
        return

    colours = {
        "broken": "red",
        "quarantine_candidate": "yellow",
        "unreliable": "yellow",
        "healthy": "green",
        "unproven": "dim",
    }
    table = Table(box=None, pad_edge=False)
    table.add_column("verdict", style="bold")
    table.add_column("test", no_wrap=True)
    table.add_column("runs", justify="right")
    table.add_column("fail", justify="right")
    table.add_column("flake", justify="right")
    table.add_column("what to do")
    for test in report.tests[:30]:
        table.add_row(
            f"[{colours.get(test.verdict, 'white')}]{test.verdict}[/]",
            (test.test_name or test.test_id)[:38],
            str(test.runs),
            str(test.failures),
            str(test.flakes),
            test.recommended_action[:54],
        )
    console.print(table)

    console.print(
        "\n[dim]A test that never passes is NOT flaky — it is reporting something. "
        "Those are never quarantined automatically.[/dim]"
    )

    if quarantine:
        result = auto_quarantine(project_id, apply=True)
        console.print(f"\n[bold]{result['summary']}[/bold]")
        for test_id in result["quarantined"]:
            console.print(f"  quarantined {test_id}")
        for test_id in result["skipped_broken"]:
            console.print(f"  [red]left alone (never passes):[/red] {test_id}")

    if fail_on_broken and report.of_verdict("broken"):
        raise typer.Exit(2)


@app.command()
def benchmark(
    runs: int = typer.Option(4, help="How many runs to measure"),
    live: bool = typer.Option(False, help="Use configured providers instead of offline mode"),
) -> None:
    """Measure the cost of repeat runs against the stated baseline."""
    from scripts.benchmark import benchmark as run_benchmark

    raise typer.Exit(asyncio.run(run_benchmark(runs=runs, live=live, keep=False)))


@app.command()
def pipeline() -> None:
    """Print the agent pipeline as Mermaid, rendered from the compiled graph."""
    from agents.orchestrator.langgraph_graph import build_orchestrator

    orchestrator = build_orchestrator()
    console.print(f"[bold]backend:[/bold] {type(orchestrator).__name__}")
    if hasattr(orchestrator, "mermaid"):
        console.print(orchestrator.mermaid())
    else:
        for name in orchestrator.nodes:
            console.print(f"  {name}")


@app.command()
def permissions() -> None:
    """Show the least-privilege matrix each agent runs under."""
    from packages.agent_protocol import describe_permissions

    table = Table("agent", "capabilities")
    for row in describe_permissions():
        table.add_row(str(row["agent"]), ", ".join(row["capabilities"]))
    console.print(table)


if __name__ == "__main__":
    app()
