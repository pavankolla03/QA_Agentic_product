"""Central runtime settings + config-file loading for the QAgentic platform.

Everything the platform needs to boot is resolved here:
  * environment variables (via .env, never exposed to agents)
  * YAML policy files in configs/ (models, standards, security)

This module is deliberately dependency-light so tools and tests can import it
without pulling in FastAPI.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

import yaml

try:  # python-dotenv is optional at import time
    from dotenv import load_dotenv

    load_dotenv(override=False)
except Exception:  # pragma: no cover
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "configs"


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_list(base: str, limit: int = 8) -> list[str]:
    """`BASE`, then `BASE_2`..`BASE_N`, in order, skipping blanks.

    Duplicates are dropped: the same key twice is one allowance, not two, and
    treating it as two would make the router think it has headroom it does not.
    """
    found: list[str] = []
    for name in (base, *(f"{base}_{n}" for n in range(2, limit + 1))):
        value = _env(name).strip()
        if value and value not in found:
            found.append(value)
    return found


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


class Settings:
    """Process-wide configuration. Instantiate once via :func:`get_settings`."""

    def __init__(self) -> None:
        self.env = _env("AIQA_ENV", "development")
        self.host = _env("AIQA_HOST", "127.0.0.1")
        self.port = _env_int("AIQA_PORT", 8080)
        self.log_level = _env("AIQA_LOG_LEVEL", "INFO")
        self.secret_key = _env("AIQA_SECRET_KEY", "dev-insecure-key")

        self.database_url = _env("AIQA_DATABASE_URL", f"sqlite:///{(REPO_ROOT / 'aiqa.db').as_posix()}")
        self.redis_url = _env("AIQA_REDIS_URL", "")

        self.bootstrap_api_key = _env("AIQA_BOOTSTRAP_API_KEY", "aiqa_dev_bootstrap_key_change_me")

        # ---- LLM providers -------------------------------------------------
        self.default_provider = _env("AIQA_DEFAULT_PROVIDER", "ollama")
        self.ollama_base_url = _env("OLLAMA_BASE_URL", "http://localhost:11434")
        self.ollama_model = _env("OLLAMA_MODEL", "qwen2.5-coder:7b")
        # A free tier is limited per key, not per account, so several keys are
        # several allowances. `OPENROUTER_API_KEY` plus any `OPENROUTER_API_KEY_2`,
        # `_3`, ... are collected in order; the first is still the primary, so
        # nothing changes for a single-key install.
        self.openrouter_api_keys = _env_list("OPENROUTER_API_KEY")
        self.openrouter_api_key = self.openrouter_api_keys[0] if self.openrouter_api_keys else ""
        self.openrouter_model = _env("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324:free")
        self.openai_api_key = _env("OPENAI_API_KEY")
        self.openai_model = _env("OPENAI_MODEL", "gpt-4o-mini")
        self.anthropic_api_key = _env("ANTHROPIC_API_KEY")
        self.anthropic_model = _env("ANTHROPIC_MODEL", "claude-sonnet-5")
        self.gemini_api_key = _env("GEMINI_API_KEY")
        self.gemini_model = _env("GEMINI_MODEL", "gemini-2.0-flash")

        # ---- Cost governance ----------------------------------------------
        self.daily_cost_limit_usd = _env_float("AIQA_DAILY_COST_LIMIT_USD", 10.0)
        self.monthly_cost_limit_usd = _env_float("AIQA_MONTHLY_COST_LIMIT_USD", 200.0)
        self.per_run_cost_limit_usd = _env_float("AIQA_PER_RUN_COST_LIMIT_USD", 2.0)
        self.max_tokens_per_run = _env_int("AIQA_MAX_TOKENS_PER_RUN", 400_000)

        # ---- Notifications -------------------------------------------------
        self.slack_webhook_url = _env("SLACK_WEBHOOK_URL")
        self.teams_webhook_url = _env("TEAMS_WEBHOOK_URL")

        # ---- Jira ----------------------------------------------------------
        self.jira_base_url = _env("JIRA_BASE_URL")
        self.jira_email = _env("JIRA_EMAIL")
        self.jira_api_token = _env("JIRA_API_TOKEN")

        # ---- Paths ----------------------------------------------------------
        self.repo_root = REPO_ROOT
        self.artifacts_dir = Path(_env("AIQA_ARTIFACTS_DIR", str(REPO_ROOT / "artifacts")))
        self.knowledge_dir = REPO_ROOT / "knowledge"

    # ------------------------------------------------------------------ #
    @property
    def configured_providers(self) -> list[str]:
        """Providers that have credentials (or need none) available."""
        out: list[str] = ["mock", "hashing"]
        if self.ollama_base_url:
            out.append("ollama")
        if self.openrouter_api_key:
            out.append("openrouter")
        if self.openai_api_key:
            out.append("openai")
        if self.anthropic_api_key:
            out.append("anthropic")
        if self.gemini_api_key:
            out.append("gemini")
        return out

    def api_key_for(self, provider: str) -> str:
        return {
            "openrouter": self.openrouter_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "gemini": self.gemini_api_key,
        }.get(provider, "")

    def to_safe_dict(self) -> dict[str, Any]:
        """Settings safe to expose over the API / to the UI (no secrets)."""
        return {
            "env": self.env,
            "host": self.host,
            "port": self.port,
            "database": self.database_url.split("://")[0],
            "default_provider": self.default_provider,
            "configured_providers": self.configured_providers,
            "cost_limits": {
                "daily_usd": self.daily_cost_limit_usd,
                "monthly_usd": self.monthly_cost_limit_usd,
                "per_run_usd": self.per_run_cost_limit_usd,
                "max_tokens_per_run": self.max_tokens_per_run,
            },
            "notifications": {
                "slack": bool(self.slack_webhook_url),
                "teams": bool(self.teams_webhook_url),
            },
        }


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# --------------------------------------------------------------------------- #
# YAML policy loading
# --------------------------------------------------------------------------- #
def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@functools.lru_cache(maxsize=1)
def load_model_config() -> dict[str, Any]:
    return _load_yaml(CONFIG_DIR / "models.yaml")


@functools.lru_cache(maxsize=1)
def load_security_config() -> dict[str, Any]:
    return _load_yaml(CONFIG_DIR / "security.yaml")


@functools.lru_cache(maxsize=1)
def load_default_standards() -> dict[str, Any]:
    return _load_yaml(CONFIG_DIR / "standards.yaml")


def load_project_standards(project_root: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Merge org defaults with a project's `.aiqa/standards.yaml`, if present.

    Project values win; ``rules`` are merged by rule id so a project can both
    add new rules and override the severity of a baseline rule.
    """
    base = dict(load_default_standards())
    if not project_root:
        return base

    override_path = Path(project_root) / ".aiqa" / "standards.yaml"
    override = _load_yaml(override_path)
    if not override:
        return base

    merged: dict[str, Any] = {**base, **{k: v for k, v in override.items() if k != "rules"}}

    by_id = {r["id"]: dict(r) for r in base.get("rules", []) if isinstance(r, dict) and "id" in r}
    for rule in override.get("rules", []) or []:
        if isinstance(rule, dict) and "id" in rule:
            by_id[rule["id"]] = {**by_id.get(rule["id"], {}), **rule}
    merged["rules"] = list(by_id.values())
    merged["_source"] = str(override_path)
    return merged


def reset_config_cache() -> None:
    """Used by tests after mutating environment or config files."""
    get_settings.cache_clear()
    load_model_config.cache_clear()
    load_security_config.cache_clear()
    load_default_standards.cache_clear()
