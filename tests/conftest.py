"""Shared pytest fixtures.

Two rules the whole suite depends on:

* Every test gets its **own SQLite file** in a tmp dir, so tests never share
  state and can run in any order.
* Any test that writes to a repository gets a **copy** of the sample fixture,
  never the fixture itself — an agent writing into ``tests/fixtures/sample-repo``
  would silently corrupt every later test.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SAMPLE_REPO = REPO_ROOT / "tests" / "fixtures" / "sample-repo"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every test at a private database and artifacts directory."""
    db_path = tmp_path / "aiqa-test.db"
    monkeypatch.setenv("AIQA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("AIQA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AIQA_BOOTSTRAP_API_KEY", "aiqa_test_key_abcdefghijklmnop")
    monkeypatch.setenv("AIQA_SECRET_KEY", "test-secret")
    monkeypatch.setenv("AIQA_PER_RUN_COST_LIMIT_USD", "5.0")
    monkeypatch.setenv("AIQA_DAILY_COST_LIMIT_USD", "100.0")
    monkeypatch.setenv("AIQA_MONTHLY_COST_LIMIT_USD", "1000.0")
    # Never let a developer's real credentials leak into a test run.
    for provider_key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(provider_key, raising=False)

    from configs.settings import reset_config_cache
    from packages.security.redaction import reset_redactor
    from services.model_router.router import reset_router
    from services.observability.db import init_db, reset_db_state

    reset_db_state()
    reset_config_cache()
    reset_redactor()
    reset_router()
    init_db()

    yield tmp_path

    reset_db_state()
    reset_config_cache()


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """A disposable copy of the sample QA repository."""
    target = tmp_path / "acme-web-e2e"
    shutil.copytree(SAMPLE_REPO, target)
    return target


@pytest.fixture
def org_user(isolated_env: Path) -> tuple[str, str]:
    from packages.aiqa_types.models import new_id
    from services.observability.db import session_scope
    from services.observability.models import OrgRow, UserRow

    org_id, user_id = new_id("org"), new_id("usr")
    with session_scope() as session:
        session.add(OrgRow(id=org_id, name="Test Org"))
        session.add(UserRow(id=user_id, org_id=org_id, email="qa@test.local", role="lead"))
    return org_id, user_id


@pytest.fixture
def project(org_user: tuple[str, str], repo_copy: Path) -> "Project":  # noqa: F821
    from packages.aiqa_types.models import Project, new_id
    from services.observability.db import session_scope
    from services.observability.models import ProjectRow

    org_id, _ = org_user
    project_id = new_id("prj")
    with session_scope() as session:
        session.add(
            ProjectRow(
                id=project_id, org_id=org_id, name="acme-web-e2e",
                repository_path=str(repo_copy),
                base_url="http://127.0.0.1:59999",     # deliberately unreachable
                framework="playwright-bdd-pom", language="typescript",
                per_run_cost_limit_usd=5.0,
            )
        )
    return Project(
        id=project_id, org_id=org_id, name="acme-web-e2e",
        repository_path=str(repo_copy), base_url="http://127.0.0.1:59999",
    )


@pytest.fixture
def offline_router() -> "ModelRouter":  # noqa: F821
    from services.model_router.router import ModelRouter

    return ModelRouter(offline=True)


@pytest.fixture
def engine() -> "AgentEngine":  # noqa: F821
    from services.agent_engine.engine import AgentEngine

    return AgentEngine(offline=True)


@pytest.fixture
def tool_ctx(repo_copy: Path) -> "ToolContext":  # noqa: F821
    from packages.observability_sdk import NullTracker
    from tools import ToolContext

    return ToolContext(
        project_root=str(repo_copy),
        project_id="prj_test",
        run_id="run_test",
        tracker=NullTracker(),
        metadata={"base_url": "http://127.0.0.1:59999"},
    )


@pytest.fixture
def registry(tool_ctx: "ToolContext") -> "ToolRegistry":  # noqa: F821
    from tools import build_registry

    return build_registry(tool_ctx)


@pytest.fixture
def agent_ctx(project: "Project", repo_copy: Path, offline_router, registry) -> "AgentContext":  # noqa: F821
    """A ready-to-use AgentContext wired to the offline router."""
    from agents.base import AgentContext
    from configs.settings import load_project_standards
    from packages.observability_sdk import NullTracker
    from services.model_router.router import RouterBudget

    return AgentContext(
        run_id="run_test",
        project=project,
        instruction="Automate the Resident Registration functionality",
        router=offline_router,
        tools=registry,
        tracker=NullTracker(),
        budget=RouterBudget(max_cost_usd=5.0, max_tokens=500_000),
        standards=load_project_standards(str(repo_copy)),
    )


@pytest.fixture
def api_client(isolated_env: Path):
    """FastAPI TestClient with a bootstrapped admin key."""
    from fastapi.testclient import TestClient

    import services.api_gateway.app as app_module
    from services.agent_engine.engine import AgentEngine

    with TestClient(app_module.app) as client:
        app_module.app.state.engine = AgentEngine(offline=True)
        client.headers.update({"X-API-Key": "aiqa_test_key_abcdefghijklmnop"})
        yield client
