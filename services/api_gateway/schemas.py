"""Request/response schemas for the HTTP API.

Kept separate from the domain models so the wire contract can evolve without
forcing changes inside the agent layer.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from packages.aiqa_types.enums import RunMode


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    repository_path: str = Field(min_length=1)
    repository_url: str | None = None
    default_branch: str = "main"
    framework: str = "playwright-bdd-pom"
    language: str = "typescript"
    base_url: str | None = None
    api_base_url: str | None = None
    database_dsn_ref: str | None = Field(
        default=None,
        description="Name of an environment variable holding the DSN — never the DSN itself",
    )
    tags: list[str] = Field(default_factory=list)
    per_run_cost_limit_usd: float = 2.0
    standards_override: dict[str, Any] = Field(default_factory=dict)

    @field_validator("database_dsn_ref")
    @classmethod
    def _reject_inline_dsn(cls, value: str | None) -> str | None:
        if value and "://" in value:
            raise ValueError(
                "database_dsn_ref must be the NAME of an environment variable, not a connection string"
            )
        return value


class ProjectUpdate(BaseModel):
    name: str | None = None
    repository_path: str | None = None
    repository_url: str | None = None
    default_branch: str | None = None
    base_url: str | None = None
    api_base_url: str | None = None
    database_dsn_ref: str | None = None
    tags: list[str] | None = None
    per_run_cost_limit_usd: float | None = None
    standards_override: dict[str, Any] | None = None


class ProjectOut(BaseModel):
    id: str
    org_id: str
    name: str
    repository_path: str
    repository_url: str = ""
    default_branch: str = "main"
    framework: str = ""
    language: str = ""
    base_url: str = ""
    api_base_url: str = ""
    database_dsn_ref: str = ""
    tags: list[str] = Field(default_factory=list)
    per_run_cost_limit_usd: float = 2.0
    indexed: bool = False
    created_at: str = ""


class ChatIn(BaseModel):
    """One message typed into the chat panel."""

    project_id: str = ""
    message: str = Field(min_length=1, max_length=20000)


class ChatOut(BaseModel):
    """What to do with it.

    `kind` is "reply" when the answer is in `text`, and "run" when the caller
    should start a run with `mode`. Turning every message into a run is what
    made "Hi" take five minutes and produce nothing.
    """

    kind: str
    text: str = ""
    mode: RunMode = RunMode.FULL
    suggestions: list[str] = Field(default_factory=list)
    #: The application the message pointed at, when it named one. The caller
    #: passes this straight back as `RunCreate.target_url` so that "automate
    #: https://shop.example.com" reaches the crawler as a URL rather than as a
    #: sentence somebody has to re-read.
    target_url: str = ""
    #: True when the scope of the run is whatever the crawl finds, rather than
    #: what the message asked for. The UI says so before starting, because
    #: "automate everything" is worth confirming.
    autopilot: bool = False


class RunCreate(BaseModel):
    project_id: str
    instruction: str = Field(min_length=1, max_length=20000)
    mode: RunMode = RunMode.FULL
    target_url: str | None = None
    jira_issue: str | None = None
    tags: list[str] = Field(default_factory=list)
    test_filter: str | None = None
    max_cost_usd: float | None = None
    auto_approve: bool = Field(
        default=False,
        description="Skip human gates. Intended for CI; the audit log records it either way.",
    )
    start: bool = Field(default=True, description="Begin executing immediately")
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunSummary(BaseModel):
    id: str
    project_id: str
    project_name: str = ""
    instruction: str
    mode: str
    status: str
    current_agent: str = ""
    progress: float = 0.0
    scenarios: int = 0
    files_changed: int = 0
    tests_total: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    llm_calls: int = 0
    duration_s: float = 0.0
    error: str = ""
    pending_approval_id: str = ""
    created_at: str = ""
    ended_at: str = ""


class RunDetail(RunSummary):
    requirement: dict[str, Any] | None = None
    test_plan: dict[str, Any] | None = None
    code_bundle: dict[str, Any] | None = None
    standards_report: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    exploration: dict[str, Any] | None = None
    analyses: list[dict[str, Any]] = Field(default_factory=list)
    heals: list[dict[str, Any]] = Field(default_factory=list)
    report: dict[str, Any] | None = None
    traces: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    visited: list[str] = Field(default_factory=list)


class ApprovalOut(BaseModel):
    id: str
    run_id: str
    project_id: str = ""
    kind: str
    title: str
    description: str = ""
    risk: str = "medium"
    payload: dict[str, Any] = Field(default_factory=dict)
    diff_preview: str = ""
    status: str = "pending"
    created_at: str = ""


class ApprovalDecision(BaseModel):
    approved: bool
    comment: str = ""
    changes_requested: bool = False


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: str = "engineer"
    user_id: str | None = None


class IndexRequest(BaseModel):
    project_id: str


class LintRequest(BaseModel):
    project_id: str
    paths: list[str] = Field(default_factory=list)


class HealthOut(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    env: str = "development"
    database: str = "sqlite"
    configured_providers: list[str] = Field(default_factory=list)
    active_routes: dict[str, Any] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)
