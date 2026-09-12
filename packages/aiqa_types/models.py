"""Domain models for the AI QA platform.

These Pydantic models are the contract between agents, tools, the API gateway
and the VS Code extension. Nothing crosses a boundary as a loose ``dict``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.aiqa_types.enums import (
    AgentName,
    AgentStatus,
    ApprovalKind,
    ApprovalStatus,
    ArtifactKind,
    Capability,
    ChangeType,
    FailureCategory,
    HealStrategy,
    Priority,
    RiskLevel,
    RunMode,
    RunStatus,
    Severity,
    TestLayer,
    TestStatus,
    ToolCategory,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Base(BaseModel):
    model_config = ConfigDict(use_enum_values=False, populate_by_name=True, extra="allow")


# =========================================================================== #
# Identity / tenancy
# =========================================================================== #
class Organization(Base):
    id: str = Field(default_factory=lambda: new_id("org"))
    name: str
    standards_ref: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class User(Base):
    id: str = Field(default_factory=lambda: new_id("usr"))
    org_id: str
    email: str
    display_name: str = ""
    role: str = "engineer"
    created_at: datetime = Field(default_factory=utcnow)


class Project(Base):
    """A QA automation repository registered with the platform."""

    id: str = Field(default_factory=lambda: new_id("prj"))
    org_id: str
    name: str
    repository_path: str
    repository_url: str | None = None
    default_branch: str = "main"
    framework: str = "playwright-bdd-pom"
    language: str = "typescript"
    base_url: str | None = None            # the app-under-test
    api_base_url: str | None = None
    database_dsn_ref: str | None = None    # name of an env var, never the DSN itself
    standards_override: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    per_run_cost_limit_usd: float = 2.0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# =========================================================================== #
# Requirements
# =========================================================================== #
class AcceptanceCriterion(Base):
    id: str = Field(default_factory=lambda: new_id("ac"))
    text: str
    testable: bool = True
    rationale: str = ""


class Requirement(Base):
    """Structured output of the Requirement Agent."""

    id: str = Field(default_factory=lambda: new_id("req"))
    raw_input: str
    title: str
    summary: str = ""
    feature_area: str = ""
    actors: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    data_requirements: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    ambiguity_score: float = 0.0          # 0 = crystal clear, 1 = unusable
    source: Literal["chat", "jira", "file", "api"] = "chat"
    source_ref: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("ambiguity_score")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


# =========================================================================== #
# Repository understanding
# =========================================================================== #
class RepoSymbol(Base):
    """A reusable element discovered in the target repository."""

    name: str
    kind: Literal["page_object", "fixture", "util", "step", "component", "type", "config"]
    file_path: str
    line: int = 0
    signature: str = ""
    exported: bool = True
    summary: str = ""
    members: list[str] = Field(default_factory=list)


class RepoProfile(Base):
    """What the Repository Understanding Agent learned about a project."""

    id: str = Field(default_factory=lambda: new_id("repo"))
    project_id: str
    root: str
    language: str = "typescript"
    test_runner: str = "playwright"
    bdd: bool = False
    package_manager: str = "npm"
    detected_layout: dict[str, str] = Field(default_factory=dict)
    frameworks: list[str] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    symbols: list[RepoSymbol] = Field(default_factory=list)
    existing_features: list[str] = Field(default_factory=list)
    naming_conventions: dict[str, str] = Field(default_factory=dict)
    file_count: int = 0
    indexed_chunks: int = 0
    conventions_summary: str = ""
    indexed_at: datetime = Field(default_factory=utcnow)

    def find_symbol(self, name: str, kind: str | None = None) -> RepoSymbol | None:
        """Locate a reusable symbol.

        Exact-case matches win over case-insensitive ones: a repository can
        legitimately contain both a ``LoginPage`` class and a ``loginPage``
        fixture, and returning the wrong one would make the Code Generation
        Agent reuse the wrong thing.
        """
        candidates = [s for s in self.symbols if kind is None or s.kind == kind]
        for sym in candidates:
            if sym.name == name:
                return sym
        lowered = name.lower()
        for sym in candidates:
            if sym.name.lower() == lowered:
                return sym
        return None

    def symbols_of(self, kind: str) -> list[RepoSymbol]:
        return [s for s in self.symbols if s.kind == kind]


# =========================================================================== #
# Application exploration
# =========================================================================== #
class DiscoveredElement(Base):
    """One interactive element found by the Application Exploration Agent."""

    role: str = ""
    name: str = ""
    tag: str = ""
    test_id: str | None = None
    label: str | None = None
    placeholder: str | None = None
    text: str | None = None
    input_type: str | None = None
    required: bool = False
    recommended_locator: str = ""
    locator_strategy: str = ""
    confidence: float = 0.0
    alternatives: list[str] = Field(default_factory=list)


class PageSnapshot(Base):
    id: str = Field(default_factory=lambda: new_id("snap"))
    url: str
    title: str = ""
    route_pattern: str = ""
    elements: list[DiscoveredElement] = Field(default_factory=list)
    forms: list[dict[str, Any]] = Field(default_factory=list)
    navigations: list[str] = Field(default_factory=list)
    screenshot_path: str | None = None
    dom_hash: str = ""
    captured_at: datetime = Field(default_factory=utcnow)


class WorkflowStep(Base):
    order: int
    action: str                 # navigate | fill | click | select | assert | wait | upload
    target: str = ""
    value: str | None = None
    description: str = ""
    locator: str | None = None


class DiscoveredWorkflow(Base):
    """An end-to-end user journey the exploration agent identified."""

    id: str = Field(default_factory=lambda: new_id("wf"))
    name: str
    description: str = ""
    entry_url: str = ""
    steps: list[WorkflowStep] = Field(default_factory=list)
    pages_touched: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class ExplorationResult(Base):
    id: str = Field(default_factory=lambda: new_id("exp"))
    project_id: str
    base_url: str = ""
    snapshots: list[PageSnapshot] = Field(default_factory=list)
    workflows: list[DiscoveredWorkflow] = Field(default_factory=list)
    unreachable: list[str] = Field(default_factory=list)
    notes: str = ""
    simulated: bool = False      # True when Playwright was unavailable
    explored_at: datetime = Field(default_factory=utcnow)


# =========================================================================== #
# Test design
# =========================================================================== #
class GherkinStep(Base):
    keyword: Literal["Given", "When", "Then", "And", "But", "*"] = "Given"
    text: str

    def render(self) -> str:
        return f"{self.keyword} {self.text}"


class Scenario(Base):
    id: str = Field(default_factory=lambda: new_id("sc"))
    test_id: str = ""                      # e.g. TC-REG-001
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    steps: list[GherkinStep] = Field(default_factory=list)
    examples: list[dict[str, str]] = Field(default_factory=list)
    priority: Priority = Priority.P2
    layer: TestLayer = TestLayer.UI
    negative: bool = False
    data_driven: bool = False
    covers_criteria: list[str] = Field(default_factory=list)
    estimated_runtime_s: int = 30


class FeatureSpec(Base):
    id: str = Field(default_factory=lambda: new_id("feat"))
    name: str
    file_name: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    background: list[GherkinStep] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)

    def to_gherkin(self) -> str:
        lines: list[str] = []
        if self.tags:
            lines.append(" ".join(self.tags))
        lines.append(f"Feature: {self.name}")
        if self.description:
            lines.extend(f"  {ln}" for ln in self.description.strip().splitlines())
        if self.background:
            lines.append("")
            lines.append("  Background:")
            lines.extend(f"    {s.render()}" for s in self.background)
        for sc in self.scenarios:
            lines.append("")
            if sc.tags:
                lines.append("  " + " ".join(sc.tags))
            keyword = "Scenario Outline" if sc.examples else "Scenario"
            title = f"{sc.test_id} {sc.name}".strip()
            lines.append(f"  {keyword}: {title}")
            lines.extend(f"    {s.render()}" for s in sc.steps)
            if sc.examples:
                headers = list(sc.examples[0].keys())
                lines.append("")
                lines.append("    Examples:")
                lines.append("      | " + " | ".join(headers) + " |")
                for row in sc.examples:
                    lines.append("      | " + " | ".join(str(row.get(h, "")) for h in headers) + " |")
        return "\n".join(lines) + "\n"


class TestPlan(Base):
    """The artifact a human approves before any code is written."""

    id: str = Field(default_factory=lambda: new_id("plan"))
    run_id: str = ""
    requirement_id: str = ""
    title: str = ""
    strategy: str = ""
    features: list[FeatureSpec] = Field(default_factory=list)
    page_objects_needed: list[str] = Field(default_factory=list)
    page_objects_reused: list[str] = Field(default_factory=list)
    fixtures_reused: list[str] = Field(default_factory=list)
    api_checks: list[str] = Field(default_factory=list)
    db_checks: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    coverage_notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def scenario_count(self) -> int:
        return sum(len(f.scenarios) for f in self.features)


# =========================================================================== #
# Code generation
# =========================================================================== #
class FileChange(Base):
    """A single proposed change to the user's workspace. Never applied without approval."""

    path: str                               # repo-relative, POSIX separators
    change_type: ChangeType = ChangeType.CREATE
    kind: ArtifactKind = ArtifactKind.PAGE_OBJECT
    content: str = ""
    original_content: str | None = None
    language: str = "typescript"
    rationale: str = ""
    reuses: list[str] = Field(default_factory=list)
    diff: str = ""
    bytes: int = 0

    def model_post_init(self, __context: Any) -> None:
        if not self.bytes:
            object.__setattr__(self, "bytes", len(self.content.encode("utf-8")))


class CodeBundle(Base):
    id: str = Field(default_factory=lambda: new_id("bundle"))
    run_id: str = ""
    plan_id: str = ""
    changes: list[FileChange] = Field(default_factory=list)
    summary: str = ""
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def total_bytes(self) -> int:
        return sum(c.bytes for c in self.changes)


# =========================================================================== #
# Standards / governance
# =========================================================================== #
class StandardsViolation(Base):
    rule_id: str
    severity: Severity = Severity.WARNING
    title: str = ""
    message: str = ""
    file_path: str = ""
    line: int = 0
    snippet: str = ""
    suggestion: str = ""
    autofixable: bool = False


class StandardsReport(Base):
    id: str = Field(default_factory=lambda: new_id("std"))
    run_id: str = ""
    passed: bool = True
    violations: list[StandardsViolation] = Field(default_factory=list)
    files_checked: int = 0
    rules_applied: int = 0
    autofixed: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utcnow)

    @property
    def error_count(self) -> int:
        return sum(1 for v in self.violations if v.severity in (Severity.ERROR, Severity.CRITICAL))

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == Severity.WARNING)


# =========================================================================== #
# Execution
# =========================================================================== #
class TestCaseResult(Base):
    test_id: str = ""
    name: str
    file_path: str = ""
    status: TestStatus = TestStatus.PASSED
    duration_ms: int = 0
    retries: int = 0
    error_message: str = ""
    error_stack: str = ""
    failed_step: str = ""
    failed_locator: str = ""
    screenshot_path: str | None = None
    video_path: str | None = None
    trace_path: str | None = None
    stdout: str = ""
    tags: list[str] = Field(default_factory=list)


class ExecutionResult(Base):
    id: str = Field(default_factory=lambda: new_id("exec"))
    run_id: str = ""
    command: str = ""
    cwd: str = ""
    exit_code: int = 0
    duration_ms: int = 0
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    flaky: int = 0
    results: list[TestCaseResult] = Field(default_factory=list)
    report_path: str | None = None
    artifacts_dir: str | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    simulated: bool = False
    started_at: datetime = Field(default_factory=utcnow)

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total) if self.total else 0.0

    @property
    def failures(self) -> list[TestCaseResult]:
        return [r for r in self.results if r.status in (TestStatus.FAILED, TestStatus.TIMED_OUT)]


# =========================================================================== #
# Failure analysis & self-healing
# =========================================================================== #
class FailureAnalysis(Base):
    id: str = Field(default_factory=lambda: new_id("fa"))
    run_id: str = ""
    test_id: str = ""
    test_name: str = ""
    file_path: str = ""
    category: FailureCategory = FailureCategory.UNKNOWN
    confidence: float = 0.0
    root_cause: str = ""
    evidence: list[str] = Field(default_factory=list)
    failed_locator: str = ""
    suggested_strategy: HealStrategy = HealStrategy.NO_ACTION
    healable: bool = False
    is_product_defect: bool = False
    defect_summary: str = ""
    recommended_action: str = ""
    analyzed_at: datetime = Field(default_factory=utcnow)


class HealProposal(Base):
    """A self-healing patch. Applied only after human (or policy) approval."""

    id: str = Field(default_factory=lambda: new_id("heal"))
    run_id: str = ""
    analysis_id: str = ""
    test_id: str = ""
    strategy: HealStrategy = HealStrategy.NO_ACTION
    file_path: str = ""
    old_snippet: str = ""
    new_snippet: str = ""
    diff: str = ""
    explanation: str = ""
    confidence: float = 0.0
    risk: RiskLevel = RiskLevel.MEDIUM
    verified: bool = False           # re-run passed after applying
    applied: bool = False
    reverted: bool = False
    created_at: datetime = Field(default_factory=utcnow)


# =========================================================================== #
# Approvals (human-in-the-loop gate)
# =========================================================================== #
class ApprovalRequest(Base):
    id: str = Field(default_factory=lambda: new_id("apr"))
    run_id: str
    project_id: str = ""
    kind: ApprovalKind = ApprovalKind.CODE_WRITE
    title: str = ""
    description: str = ""
    risk: RiskLevel = RiskLevel.MEDIUM
    payload: dict[str, Any] = Field(default_factory=dict)
    diff_preview: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_by: str = "orchestrator"
    responded_by: str | None = None
    response_comment: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    responded_at: datetime | None = None
    expires_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.status == ApprovalStatus.PENDING


# =========================================================================== #
# Observability / cost
# =========================================================================== #
class TokenUsage(Base):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )


class LLMCallTrace(Base):
    id: str = Field(default_factory=lambda: new_id("llm"))
    run_id: str = ""
    trace_id: str = ""
    agent: AgentName | None = None
    provider: str = ""
    model: str = ""
    capability: Capability = Capability.FAST
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    latency_ms: int = 0
    status: str = "succeeded"
    error: str = ""
    prompt_chars: int = 0
    completion_chars: int = 0
    prompt_preview: str = ""       # redacted, truncated
    fallback_from: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ToolCallTrace(Base):
    id: str = Field(default_factory=lambda: new_id("tc"))
    run_id: str = ""
    trace_id: str = ""
    agent: AgentName | None = None
    category: ToolCategory = ToolCategory.FILESYSTEM
    tool: str = ""
    arguments_preview: str = ""
    status: str = "succeeded"
    error: str = ""
    latency_ms: int = 0
    result_preview: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class AgentTrace(Base):
    id: str = Field(default_factory=lambda: new_id("trc"))
    run_id: str = ""
    session_id: str = ""
    project_id: str = ""
    user_id: str = ""
    repository_id: str = ""
    agent: AgentName = AgentName.ORCHESTRATOR
    status: AgentStatus = AgentStatus.PENDING
    sequence: int = 0
    input_summary: str = ""
    output_summary: str = ""
    provider: str = ""
    model: str = ""
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    latency_ms: int = 0
    llm_calls: int = 0
    tool_calls: list[str] = Field(default_factory=list)
    error: str = ""
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None


class CostSummary(Base):
    run_id: str = ""
    project_id: str = ""
    user_id: str = ""
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    by_agent: dict[str, float] = Field(default_factory=dict)
    by_model: dict[str, float] = Field(default_factory=dict)
    by_provider: dict[str, float] = Field(default_factory=dict)
    free_calls: int = 0
    paid_calls: int = 0


class AuditEntry(Base):
    id: str = Field(default_factory=lambda: new_id("aud"))
    org_id: str = ""
    project_id: str = ""
    run_id: str = ""
    user_id: str = ""
    action: str = ""
    resource: str = ""
    outcome: str = "allowed"
    detail: str = ""
    ip: str = ""
    created_at: datetime = Field(default_factory=utcnow)


# =========================================================================== #
# Runs
# =========================================================================== #
class RunRequest(Base):
    """What the VS Code extension posts to start work."""

    project_id: str
    instruction: str
    mode: RunMode = RunMode.FULL
    target_url: str | None = None
    jira_issue: str | None = None
    tags: list[str] = Field(default_factory=list)
    test_filter: str | None = None
    max_cost_usd: float | None = None
    auto_approve: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunReport(Base):
    id: str = Field(default_factory=lambda: new_id("rpt"))
    run_id: str = ""
    title: str = ""
    headline: str = ""
    markdown: str = ""
    html: str = ""
    scenarios_designed: int = 0
    files_changed: int = 0
    tests_total: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    heals_applied: int = 0
    product_defects: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    duration_s: float = 0.0
    next_actions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class Run(Base):
    """The top-level unit of work. One instruction in → one Run."""

    id: str = Field(default_factory=lambda: new_id("run"))
    org_id: str = ""
    project_id: str = ""
    user_id: str = ""
    session_id: str = Field(default_factory=lambda: new_id("ses"))
    repository_id: str = ""
    instruction: str = ""
    mode: RunMode = RunMode.FULL
    status: RunStatus = RunStatus.QUEUED
    current_agent: AgentName | None = None
    progress: float = 0.0
    requirement: Requirement | None = None
    test_plan: TestPlan | None = None
    code_bundle: CodeBundle | None = None
    standards_report: StandardsReport | None = None
    execution: ExecutionResult | None = None
    analyses: list[FailureAnalysis] = Field(default_factory=list)
    heals: list[HealProposal] = Field(default_factory=list)
    report: RunReport | None = None
    traces: list[AgentTrace] = Field(default_factory=list)
    approvals: list[ApprovalRequest] = Field(default_factory=list)
    cost: CostSummary = Field(default_factory=CostSummary)
    error: str = ""
    iteration: int = 0
    max_iterations: int = 3
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @property
    def duration_s(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.ended_at or utcnow()
        return (end - self.started_at).total_seconds()


# =========================================================================== #
# Streaming events (WebSocket → VS Code)
# =========================================================================== #
class RunEvent(Base):
    """A single event streamed to the extension / control plane."""

    id: str = Field(default_factory=lambda: new_id("evt"))
    run_id: str
    type: str                # agent_started | agent_finished | log | approval_required | ...
    agent: AgentName | None = None
    level: Severity = Severity.INFO
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    progress: float | None = None
    at: datetime = Field(default_factory=utcnow)


__all__ = [n for n in dir() if n[0].isupper()] + ["new_id", "utcnow"]
