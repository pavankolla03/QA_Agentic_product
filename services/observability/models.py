"""SQLAlchemy ORM — the observability & governance store.

Traceability is mandatory per the spec: every run carries
``run_id / project_id / user_id / repository_id / session_id / timestamp / status``
and every agent step records model, provider, tokens, latency, cost, status,
error and tool calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# =========================================================================== #
# Tenancy & auth
# =========================================================================== #
class OrgRow(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    standards: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    daily_cost_limit_usd: Mapped[float] = mapped_column(Float, default=10.0)
    monthly_cost_limit_usd: Mapped[float] = mapped_column(Float, default=200.0)

    projects: Mapped[list[ProjectRow]] = relationship(back_populates="org", cascade="all, delete-orphan")
    users: Mapped[list[UserRow]] = relationship(back_populates="org", cascade="all, delete-orphan")


class UserRow(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_user_org_email"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(32), default="engineer")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    org: Mapped[OrgRow] = relationship(back_populates="users")


class ApiKeyRow(Base, TimestampMixin):
    """API keys are stored only as salted hashes."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(120), default="default")
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16), default="")
    role: Mapped[str] = mapped_column(String(32), default="engineer")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# =========================================================================== #
# Projects
# =========================================================================== #
class ProjectRow(Base, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_project_org_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    repository_path: Mapped[str] = mapped_column(Text, nullable=False)
    repository_url: Mapped[str] = mapped_column(Text, default="")
    default_branch: Mapped[str] = mapped_column(String(120), default="main")
    framework: Mapped[str] = mapped_column(String(80), default="playwright-bdd-pom")
    language: Mapped[str] = mapped_column(String(40), default="typescript")
    base_url: Mapped[str] = mapped_column(Text, default="")
    api_base_url: Mapped[str] = mapped_column(Text, default="")
    database_dsn_ref: Mapped[str] = mapped_column(String(120), default="")
    standards_override: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    repo_profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    per_run_cost_limit_usd: Mapped[float] = mapped_column(Float, default=2.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    org: Mapped[OrgRow] = relationship(back_populates="projects")
    runs: Mapped[list[RunRow]] = relationship(back_populates="project", cascade="all, delete-orphan")


# =========================================================================== #
# Runs & traces
# =========================================================================== #
class RunRow(Base, TimestampMixin):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_project_created", "project_id", "created_at"),
        Index("ix_runs_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    session_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    repository_id: Mapped[str] = mapped_column(String(64), default="")

    instruction: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(32), default="full")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    current_agent: Mapped[str] = mapped_column(String(40), default="")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    iteration: Mapped[int] = mapped_column(Integer, default=0)

    # Structured artifacts (stored as JSON documents for full replay)
    requirement: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    test_plan: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    code_bundle: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    standards_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    execution: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    analyses: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    heals: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    exploration: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Roll-ups
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)

    tests_total: Mapped[int] = mapped_column(Integer, default=0)
    tests_passed: Mapped[int] = mapped_column(Integer, default=0)
    tests_failed: Mapped[int] = mapped_column(Integer, default=0)
    files_changed: Mapped[int] = mapped_column(Integer, default=0)

    error: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)

    project: Mapped[ProjectRow] = relationship(back_populates="runs")
    traces: Mapped[list[AgentTraceRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentTraceRow.sequence"
    )
    llm_call_rows: Mapped[list[LLMCallRow]] = relationship(back_populates="run", cascade="all, delete-orphan")
    tool_call_rows: Mapped[list[ToolCallRow]] = relationship(back_populates="run", cascade="all, delete-orphan")
    approvals: Mapped[list[ApprovalRow]] = relationship(back_populates="run", cascade="all, delete-orphan")
    events: Mapped[list[RunEventRow]] = relationship(back_populates="run", cascade="all, delete-orphan")


class AgentTraceRow(Base, TimestampMixin):
    __tablename__ = "agent_traces"
    __table_args__ = (Index("ix_trace_run_seq", "run_id", "sequence"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="")
    session_id: Mapped[str] = mapped_column(String(64), default="")
    repository_id: Mapped[str] = mapped_column(String(64), default="")

    agent: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    sequence: Mapped[int] = mapped_column(Integer, default=0)

    input_summary: Mapped[str] = mapped_column(Text, default="")
    output_summary: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(40), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[list[str]] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[RunRow] = relationship(back_populates="traces")


class LLMCallRow(Base, TimestampMixin):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_run_agent", "run_id", "agent"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    agent: Mapped[str] = mapped_column(String(40), default="")
    provider: Mapped[str] = mapped_column(String(40), default="", index=True)
    model: Mapped[str] = mapped_column(String(120), default="", index=True)
    capability: Mapped[str] = mapped_column(String(24), default="fast")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="succeeded")
    error: Mapped[str] = mapped_column(Text, default="")
    prompt_chars: Mapped[int] = mapped_column(Integer, default=0)
    completion_chars: Mapped[int] = mapped_column(Integer, default=0)
    prompt_preview: Mapped[str] = mapped_column(Text, default="")
    fallback_from: Mapped[str] = mapped_column(String(40), default="")
    redacted_kinds: Mapped[list[str]] = mapped_column(JSON, default=list)

    run: Mapped[RunRow] = relationship(back_populates="llm_call_rows")


class ToolCallRow(Base, TimestampMixin):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    trace_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    agent: Mapped[str] = mapped_column(String(40), default="")
    category: Mapped[str] = mapped_column(String(32), default="filesystem", index=True)
    tool: Mapped[str] = mapped_column(String(80), default="", index=True)
    arguments_preview: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="succeeded")
    error: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    result_preview: Mapped[str] = mapped_column(Text, default="")

    run: Mapped[RunRow] = relationship(back_populates="tool_call_rows")


class RunEventRow(Base, TimestampMixin):
    """Append-only event log — replays a run in the UI after the fact."""

    __tablename__ = "run_events"
    __table_args__ = (Index("ix_event_run_created", "run_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(48), index=True)
    agent: Mapped[str] = mapped_column(String(40), default="")
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)

    run: Mapped[RunRow] = relationship(back_populates="events")


# =========================================================================== #
# Human-in-the-loop
# =========================================================================== #
class ApprovalRow(Base, TimestampMixin):
    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approval_status", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    risk: Mapped[str] = mapped_column(String(16), default="medium")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    diff_preview: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    requested_by: Mapped[str] = mapped_column(String(64), default="orchestrator")
    responded_by: Mapped[str] = mapped_column(String(64), default="")
    response_comment: Mapped[str] = mapped_column(Text, default="")
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[RunRow] = relationship(back_populates="approvals")


class AuditRow(Base, TimestampMixin):
    """Append-only audit trail. Never updated, never deleted."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_org_created", "org_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    project_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    run_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    resource: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(24), default="allowed", index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


# =========================================================================== #
# Cost roll-ups
# =========================================================================== #
class CostDailyRow(Base):
    """Pre-aggregated daily spend — keeps dashboards O(1)."""

    __tablename__ = "cost_daily"
    __table_args__ = (
        UniqueConstraint("day", "org_id", "project_id", "user_id", "provider", "model", name="uq_cost_daily"),
        Index("ix_cost_day", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[str] = mapped_column(String(10), index=True)          # YYYY-MM-DD
    month: Mapped[str] = mapped_column(String(7), index=True)          # YYYY-MM
    org_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    project_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    agent: Mapped[str] = mapped_column(String(40), default="")
    provider: Mapped[str] = mapped_column(String(40), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    calls: Mapped[int] = mapped_column(Integer, default=0)


# =========================================================================== #
# Knowledge store (repository understanding)
# =========================================================================== #
class KnowledgeChunkRow(Base, TimestampMixin):
    """A chunk of repository/company knowledge plus its embedding."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("ix_chunk_project_kind", "project_id", "kind"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(24), default="repository")  # company|project|repository|test
    kind: Mapped[str] = mapped_column(String(32), default="code")
    file_path: Mapped[str] = mapped_column(Text, default="")
    symbol: Mapped[str] = mapped_column(String(200), default="")
    start_line: Mapped[int] = mapped_column(Integer, default=0)
    end_line: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    embedding: Mapped[list[float]] = mapped_column(JSON, default=list)
    embedding_model: Mapped[str] = mapped_column(String(120), default="")
    tokens: Mapped[int] = mapped_column(Integer, default=0)


class ArtifactRow(Base, TimestampMixin):
    """Pointer to a file produced by a run (report, screenshot, trace, video)."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    kind: Mapped[str] = mapped_column(String(32), default="report")
    name: Mapped[str] = mapped_column(Text, default="")
    path: Mapped[str] = mapped_column(Text, default="")
    content_type: Mapped[str] = mapped_column(String(80), default="text/plain")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)


class HealHistoryRow(Base, TimestampMixin):
    """Every self-heal ever proposed — the audit trail for autonomous edits."""

    __tablename__ = "heal_history"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    test_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    file_path: Mapped[str] = mapped_column(Text, default="")
    strategy: Mapped[str] = mapped_column(String(40), default="")
    old_snippet: Mapped[str] = mapped_column(Text, default="")
    new_snippet: Mapped[str] = mapped_column(Text, default="")
    diff: Mapped[str] = mapped_column(Text, default="")
    explanation: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    risk: Mapped[str] = mapped_column(String(16), default="medium")
    approved_by: Mapped[str] = mapped_column(String(64), default="")
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    reverted: Mapped[bool] = mapped_column(Boolean, default=False)


class FlakyTestRow(Base):
    """Flakiness ledger — powers 'quarantine this test' recommendations."""

    __tablename__ = "flaky_tests"
    __table_args__ = (UniqueConstraint("project_id", "test_id", name="uq_flaky_project_test"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    test_id: Mapped[str] = mapped_column(String(120), index=True)
    test_name: Mapped[str] = mapped_column(Text, default="")
    file_path: Mapped[str] = mapped_column(Text, default="")
    runs: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    flakes: Mapped[int] = mapped_column(Integer, default=0)
    heals: Mapped[int] = mapped_column(Integer, default=0)
    last_category: Mapped[str] = mapped_column(String(40), default="")
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    @property
    def flake_rate(self) -> float:
        return (self.flakes / self.runs) if self.runs else 0.0


ALL_TABLES = [
    OrgRow, UserRow, ApiKeyRow, ProjectRow, RunRow, AgentTraceRow, LLMCallRow,
    ToolCallRow, RunEventRow, ApprovalRow, AuditRow, CostDailyRow,
    KnowledgeChunkRow, ArtifactRow, HealHistoryRow, FlakyTestRow,
]
