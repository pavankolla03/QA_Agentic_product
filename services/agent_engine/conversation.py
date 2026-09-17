"""Answering from the platform's own records.

"How many tests failed?" has an exact answer sitting in a database row. Asking
a language model to produce it is slower, costs a request, and can be wrong —
it is the one question type where a model is strictly worse than a query.

Everything here is deterministic: a lookup, some arithmetic, and a sentence.
No model is consulted, which is why these answer in tens of milliseconds rather
than seconds, and why they are still correct when every provider is rate
limited.

A handler that cannot answer returns "" and the caller falls through to a
model. That is the only escalation path, and it is one-way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select

from packages.aiqa_types.enums import RunStatus
from services.agent_engine.intents import Intent, Resolution
from services.observability.db import session_scope
from services.observability.models import (
    ApprovalRow,
    CostDailyRow,
    ProjectRow,
    RunRow,
)

HELP_TEXT = """I automate UI testing for the application this project points at.

Ask me for work in plain language, for example:
  - "automate the login page: valid sign-in and wrong password"
  - "cover the registration form, including required-field validation"
  - "run the tests and repair anything that fails"

You can also just ask me things, and I answer those from my own records
rather than by running anything:
  - "how many tests failed?"
  - "why did the last run fail?"
  - "what did today cost?"
  - "is it still running?"

On free models a full automation run takes several minutes. You will see each
agent start and finish as it goes."""

GREETING = (
    "Hello. Tell me what to automate — a page, a form, a flow — and I will take "
    "it from there. Or ask me about a previous run; I answer those instantly."
)

_SUGGESTIONS = ["automate the login page", "how many tests failed?", "what did today cost?"]


@dataclass
class Answer:
    """A reply, and whether anything else needs to happen."""

    text: str = ""
    suggestions: list[str] | None = None


class ConversationService:
    """Deterministic answers to questions about this platform's own state."""

    def __init__(self, project_id: str = "", org_id: str = "") -> None:
        self.project_id = project_id
        self.org_id = org_id

    # ------------------------------------------------------------------ #
    def answer(self, resolution: Resolution) -> Answer | None:
        """Answer if this is a question we hold the facts for, else None."""
        handler = {
            Intent.CONVERSATION: self._greeting,
            Intent.HELP: self._help,
            Intent.RUN_STATUS: self._run_status,
            Intent.RUN_FAILURE_QUERY: self._failures,
            Intent.RUN_REPORT: self._report,
            Intent.COST_QUERY: self._cost,
            Intent.PROJECT_QUERY: self._project,
            Intent.CONFIGURATION_QUERY: self._configuration,
            Intent.TEST_QUERY: self._tests,
            Intent.RUN_CANCEL: self._cancel_hint,
            Intent.RUN_RETRY: self._retry_hint,
            Intent.APPROVAL_ACCEPT: self._approval_hint,
            Intent.APPROVAL_REJECT: self._approval_hint,
        }.get(resolution.intent)
        return handler(resolution) if handler else None

    # -- conversation --------------------------------------------------- #
    def _greeting(self, _: Resolution) -> Answer:
        return Answer(GREETING, _SUGGESTIONS)

    def _help(self, _: Resolution) -> Answer:
        return Answer(HELP_TEXT)

    # -- runs ----------------------------------------------------------- #
    def _run_status(self, resolution: Resolution) -> Answer:
        run = self._run(resolution.run_id)
        if run is None:
            return Answer("No runs yet for this project. Ask me to automate something.")

        if run["status"] == RunStatus.RUNNING.value:
            stage = (run["current_agent"] or "starting").replace("_", " ")
            lines = [f"Run {run['id']} is running — currently {stage}."]
            if run["tests_total"]:
                lines.append(
                    f"{run['tests_passed']}/{run['tests_total']} scenarios passing so far."
                )
            elif run["files_changed"]:
                lines.append(f"{run['files_changed']} file(s) written so far.")
            return Answer("\n".join(lines))

        if run["status"] == RunStatus.WAITING_APPROVAL.value:
            return Answer(f"Run {run['id']} is waiting for your approval before it can continue.")

        summary = self._outcome(run)
        return Answer(f"Run {run['id']} {run['status']}. {summary}")

    def _failures(self, resolution: Resolution) -> Answer:
        run = self._run(resolution.run_id)
        if run is None:
            return Answer("No runs yet, so nothing has failed.")
        if run["error"]:
            return Answer(
                f"Run {run['id']} did not get as far as running tests.\n\n{run['error'][:400]}"
            )
        if not run["tests_total"]:
            return Answer(
                f"Run {run['id']} did not execute any tests, so there are no failures to report. "
                f"It finished as {run['status']}."
            )
        if not run["tests_failed"]:
            return Answer(f"Nothing failed — {run['tests_passed']}/{run['tests_total']} passed.")
        return Answer(
            f"{run['tests_failed']} of {run['tests_total']} scenarios failed in run {run['id']}.\n\n"
            "Ask me to heal them and I will diagnose each one and attempt a repair."
        )

    def _report(self, resolution: Resolution) -> Answer:
        run = self._run(resolution.run_id)
        if run is None:
            return Answer("No runs yet for this project.")
        lines = [
            f"Run {run['id']} — {run['status']}",
            f"  {run['instruction'][:90]}",
            f"  files written : {run['files_changed']}",
            f"  scenarios     : {run['tests_passed']}/{run['tests_total']} passing",
            f"  tokens        : {run['total_tokens']:,}",
            f"  cost          : ${run['total_cost_usd']:.4f}",
            f"  duration      : {run['duration_s']:.0f}s",
        ]
        if run["error"]:
            lines.append(f"  blocked       : {run['error'][:120]}")
        return Answer("\n".join(lines))

    # -- money ---------------------------------------------------------- #
    def _cost(self, _: Resolution) -> Answer:
        today = date.today().isoformat()
        with session_scope() as session:
            stmt = select(
                func.coalesce(func.sum(CostDailyRow.cost_usd), 0.0),
                func.coalesce(func.sum(CostDailyRow.total_tokens), 0),
                func.coalesce(func.sum(CostDailyRow.calls), 0),
            ).where(CostDailyRow.day == today)
            if self.project_id:
                stmt = stmt.where(CostDailyRow.project_id == self.project_id)
            cost, tokens, calls = session.execute(stmt).one()

            top = session.execute(
                select(CostDailyRow.model, func.sum(CostDailyRow.total_tokens).label("t"))
                .where(CostDailyRow.day == today)
                .group_by(CostDailyRow.model)
                .order_by(func.sum(CostDailyRow.total_tokens).desc())
                .limit(3)
            ).all()

        if not calls:
            return Answer("No model calls yet today, so nothing has been spent.")

        lines = [
            f"Today: ${cost:.4f} across {calls:,} model call(s) and {tokens:,} tokens."
        ]
        if cost == 0:
            lines.append("Everything ran on free models, which is why the cost is zero.")
        if top:
            lines.append("")
            lines.append("Most used:")
            lines += [f"  {model or '(unknown)'}: {int(total):,} tokens" for model, total in top]
        return Answer("\n".join(lines))

    # -- project -------------------------------------------------------- #
    def _project(self, _: Resolution) -> Answer:
        if not self.project_id:
            return Answer(
                "No project is bound to this workspace yet. Run \"AI QA: Set Up\" and I will "
                "walk through it."
            )
        with session_scope() as session:
            project = session.get(ProjectRow, self.project_id)
            if project is None:
                return Answer(f"Project {self.project_id} is configured but no longer exists on this server.")
            runs = session.execute(
                select(func.count(RunRow.id)).where(RunRow.project_id == self.project_id)
            ).scalar_one()
            details = [
                f"Project: {project.name}",
                f"Repository: {project.repository_path}",
                f"Application: {project.base_url or '(no URL set — nothing can be crawled)'}",
                f"Framework: {project.framework}",
                f"Runs so far: {runs}",
            ]
        return Answer("\n".join(details))

    def _configuration(self, _: Resolution) -> Answer:
        from configs.settings import load_model_config

        config = load_model_config()
        routes = config.get("routes", {}) or {}
        free_only = bool((config.get("defaults", {}) or {}).get("free_only"))
        lines = ["Model routing:"]
        for tier in ("interactive_chat", "reasoning", "coding", "cheap"):
            entries = routes.get(tier) or []
            if entries:
                first = entries[0]
                lines.append(f"  {tier:17s} {first.get('provider')}/{first.get('model')}")
        if free_only:
            lines.append("")
            lines.append("Paid models are refused outright, whatever a tier lists.")
        return Answer("\n".join(lines))

    def _tests(self, _: Resolution) -> Answer:
        with session_scope() as session:
            stmt = select(
                func.coalesce(func.sum(RunRow.tests_total), 0),
                func.coalesce(func.sum(RunRow.files_changed), 0),
            )
            if self.project_id:
                stmt = stmt.where(RunRow.project_id == self.project_id)
            executed, files = session.execute(stmt).one()
        return Answer(
            f"Across every run in this project: {int(files)} file(s) written and "
            f"{int(executed)} scenario execution(s) recorded."
        )

    # -- things that need a different surface --------------------------- #
    def _cancel_hint(self, resolution: Resolution) -> Answer:
        run = self._run(resolution.run_id)
        if run is None or run["status"] not in (RunStatus.RUNNING.value, RunStatus.QUEUED.value):
            return Answer("Nothing is running at the moment.")
        return Answer(f"Run {run['id']} is running. Use the Stop button above to cancel it.")

    def _retry_hint(self, resolution: Resolution) -> Answer:
        run = self._run(resolution.run_id)
        if run is None:
            return Answer("There is no previous run to retry.")
        return Answer(
            f"The last run was: \"{run['instruction'][:90]}\"\n\n"
            "Send me that again and I will start a fresh run."
        )

    def _approval_hint(self, _: Resolution) -> Answer:
        with session_scope() as session:
            pending = session.execute(
                select(func.count(ApprovalRow.id)).where(ApprovalRow.status == "pending")
            ).scalar_one()
        if not pending:
            return Answer("Nothing is waiting for approval.")
        return Answer(
            f"{pending} approval(s) are waiting. Open the Approvals view to review the diff "
            "before deciding — approving from chat without seeing the change is not something "
            "I will do for you."
        )

    # ------------------------------------------------------------------ #
    def _run(self, run_id: str = "") -> dict | None:
        """The named run, or the most recent one for this project."""
        with session_scope() as session:
            if run_id:
                row = session.get(RunRow, run_id)
            else:
                stmt = select(RunRow).order_by(RunRow.created_at.desc()).limit(1)
                if self.project_id:
                    stmt = stmt.where(RunRow.project_id == self.project_id)
                row = session.execute(stmt).scalars().first()
            if row is None:
                return None
            # Read everything out while the session is open; the caller gets a
            # plain dict rather than a detached row that raises on access.
            return {
                "id": row.id,
                "status": row.status,
                "current_agent": row.current_agent,
                "instruction": row.instruction,
                "error": row.error or "",
                "tests_total": row.tests_total,
                "tests_passed": row.tests_passed,
                "tests_failed": row.tests_failed,
                "files_changed": row.files_changed,
                "total_tokens": row.total_tokens,
                "total_cost_usd": row.total_cost_usd,
                "duration_s": row.duration_s,
            }

    @staticmethod
    def _outcome(run: dict) -> str:
        if run["error"]:
            return run["error"][:200]
        if run["tests_total"]:
            return f"{run['tests_passed']}/{run['tests_total']} scenarios passed."
        if run["files_changed"]:
            return f"{run['files_changed']} file(s) written; no tests were executed."
        return "Nothing was produced."
