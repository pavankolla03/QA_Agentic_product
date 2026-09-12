"""Canonical enumerations shared by every service, agent and tool."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """`str`-backed enum so values serialise cleanly to JSON/SQL."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


# --------------------------------------------------------------------------- #
# Runs & orchestration
# --------------------------------------------------------------------------- #
class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"

    @property
    def terminal(self) -> bool:
        return self in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.BUDGET_EXCEEDED,
        }


class RunMode(StrEnum):
    """How much autonomy the human grants for this run."""

    PLAN_ONLY = "plan_only"          # design tests, write nothing
    GENERATE = "generate"            # write code, stop before executing
    FULL = "full"                    # generate + execute + analyse
    AUTONOMOUS = "autonomous"        # full + self-heal + rerun, approvals batched
    EXECUTE_ONLY = "execute_only"    # run existing tests, analyse failures
    HEAL_ONLY = "heal_only"          # repair known-failing tests


class AgentName(StrEnum):
    ORCHESTRATOR = "orchestrator"
    REQUIREMENT = "requirement"
    REPOSITORY = "repository"
    EXPLORATION = "exploration"
    TEST_DESIGN = "test_design"
    CODE_GENERATION = "code_generation"
    STANDARDS = "standards"
    EXECUTION = "execution"
    FAILURE_ANALYSIS = "failure_analysis"
    SELF_HEALING = "self_healing"
    REPORTING = "reporting"


class AgentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING_APPROVAL = "waiting_approval"


# --------------------------------------------------------------------------- #
# Human-in-the-loop
# --------------------------------------------------------------------------- #
class ApprovalKind(StrEnum):
    TEST_PLAN = "test_plan"
    CODE_WRITE = "code_write"
    GIT_COMMIT = "git_commit"
    GIT_PUSH = "git_push"
    SELF_HEAL_APPLY = "self_heal_apply"
    DESTRUCTIVE_COMMAND = "destructive_command"
    BUDGET_INCREASE = "budget_increase"
    EXPLORATION_TARGET = "exploration_target"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"
    EXPIRED = "expired"
    AUTO_APPROVED = "auto_approved"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2, "critical": 3}[self.value]


# --------------------------------------------------------------------------- #
# Test artifacts
# --------------------------------------------------------------------------- #
class ArtifactKind(StrEnum):
    FEATURE = "feature"
    STEP_DEFINITION = "step_definition"
    PAGE_OBJECT = "page_object"
    FIXTURE = "fixture"
    UTIL = "util"
    TEST_DATA = "test_data"
    API_TEST = "api_test"
    DB_CHECK = "db_check"
    CONFIG = "config"
    REPORT = "report"


class ChangeType(StrEnum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class TestStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    FLAKY = "flaky"
    INTERRUPTED = "interrupted"


class TestLayer(StrEnum):
    UI = "ui"
    API = "api"
    DATABASE = "database"
    MOBILE = "mobile"
    INTEGRATION = "integration"


class Priority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


# --------------------------------------------------------------------------- #
# Failure analysis & healing
# --------------------------------------------------------------------------- #
class FailureCategory(StrEnum):
    """Root-cause taxonomy — drives whether self-healing may act."""

    LOCATOR_BROKEN = "locator_broken"          # element moved/renamed  -> healable
    TIMING_FLAKE = "timing_flake"              # race/wait issue        -> healable
    TEST_DATA = "test_data"                    # stale/missing data     -> healable
    ENVIRONMENT = "environment"                # env down, 5xx, DNS     -> not a bug
    NETWORK = "network"                        # transient network      -> retry
    ASSERTION_MISMATCH = "assertion_mismatch"  # expected != actual     -> maybe app bug
    APPLICATION_BUG = "application_bug"        # genuine defect         -> DO NOT heal
    TEST_LOGIC_ERROR = "test_logic_error"      # bad test               -> healable
    CONFIGURATION = "configuration"            # config/setup issue
    DEPENDENCY = "dependency"                  # upstream service/fixture
    UNKNOWN = "unknown"

    @property
    def healable(self) -> bool:
        return self in {
            FailureCategory.LOCATOR_BROKEN,
            FailureCategory.TIMING_FLAKE,
            FailureCategory.TEST_DATA,
            FailureCategory.TEST_LOGIC_ERROR,
        }

    @property
    def is_product_defect(self) -> bool:
        return self in {FailureCategory.APPLICATION_BUG, FailureCategory.ASSERTION_MISMATCH}


class HealStrategy(StrEnum):
    RELOCATE_SELECTOR = "relocate_selector"
    ADD_EXPLICIT_WAIT = "add_explicit_wait"
    REPLACE_HARD_WAIT = "replace_hard_wait"
    REFRESH_TEST_DATA = "refresh_test_data"
    UPDATE_EXPECTED_VALUE = "update_expected_value"
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    FIX_STEP_LOGIC = "fix_step_logic"
    NO_ACTION = "no_action"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# --------------------------------------------------------------------------- #
# LLM layer
# --------------------------------------------------------------------------- #
class Capability(StrEnum):
    FAST = "fast"
    REASONING = "reasoning"
    CODING = "coding"
    EMBEDDING = "embedding"


class ProviderName(StrEnum):
    OLLAMA = "ollama"
    OPENROUTER = "openrouter"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    MLX = "mlx"
    HASHING = "hashing"
    MOCK = "mock"


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


# --------------------------------------------------------------------------- #
# Tools & audit
# --------------------------------------------------------------------------- #
class ToolCategory(StrEnum):
    FILESYSTEM = "filesystem"
    GIT = "git"
    PLAYWRIGHT = "playwright"
    API = "api"
    DATABASE = "database"
    MOBILE = "mobile"
    JIRA = "jira"
    SLACK = "slack"
    TEAMS = "teams"
    SHELL = "shell"
    CI = "ci"


class AuditAction(StrEnum):
    LOGIN = "login"
    PROJECT_CREATE = "project_create"
    PROJECT_UPDATE = "project_update"
    RUN_CREATE = "run_create"
    RUN_CANCEL = "run_cancel"
    APPROVAL_GRANT = "approval_grant"
    APPROVAL_REJECT = "approval_reject"
    FILE_WRITE = "file_write"
    GIT_COMMIT = "git_commit"
    GIT_PUSH = "git_push"
    COMMAND_EXEC = "command_exec"
    SECRET_BLOCKED = "secret_blocked"
    POLICY_VIOLATION = "policy_violation"
    BUDGET_BLOCK = "budget_block"
    STANDARDS_OVERRIDE = "standards_override"


class Role_RBAC(StrEnum):
    VIEWER = "viewer"
    ENGINEER = "engineer"
    LEAD = "lead"
    ADMIN = "admin"
