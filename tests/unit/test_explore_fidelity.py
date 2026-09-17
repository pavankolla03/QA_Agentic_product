"""The crawler must not upgrade somebody else's "unverified" to "verified".

The browser script has its own last-resort fallback: when no browser will start
it reads the HTML over plain HTTP, sets `simulated: true` and names itself
`http (no browser)`. It is being scrupulous — nothing on those pages was
rendered or clicked, so no locator taken from them has been verified.

The Python wrapper then hardcoded `"simulated": False` over that. A crawl the
script had explicitly labelled unverified was handed on as a verified browser
crawl, and every locator from it was trusted for code generation. Of all the
shapes this class of bug takes, this is the worst one: the truth was already
computed, already in the payload, and was overwritten on the way past.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tools.base import ToolResult
from tools.playwright import pw_tools
from tools.playwright.pw_tools import BEGIN, END, ExploreAppTool


class _Ctx:
    """Everything the tool reads off a context, and nothing else."""

    def __init__(self, tmp_path: Any) -> None:
        self.project_root = str(tmp_path)
        self.tracker = None
        self.run_id = "r"
        self.dry_run = False
        self.budget = None
        self.project = None


def _script_said(**payload: Any) -> dict[str, Any]:
    body = {
        "base_url": "http://app.test",
        "snapshots": [
            {
                "url": "http://app.test/login",
                "title": "Sign in",
                "elements": [],
                "forms": [],
                "navigations": [],
                "screenshot_path": None,
            }
        ],
        "unreachable": [],
        "errors": [],
        "simulated": False,
        "browser": "bundled chromium",
    }
    body.update(payload)
    return {"stdout": f"\n{BEGIN}\n{json.dumps(body)}\n{END}\n"}


@pytest.fixture
def tool(tmp_path, monkeypatch: pytest.MonkeyPatch) -> ExploreAppTool:
    instance = ExploreAppTool()
    instance.ctx = _Ctx(tmp_path)
    monkeypatch.setattr(
        pw_tools, "probe_toolchain",
        lambda _root: {"node": True, "playwright_installed": True},
    )
    return instance


def _stub_node(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    monkeypatch.setattr(
        pw_tools.RunCommandTool, "run",
        lambda self, **_kwargs: ToolResult.success(payload),
    )


# --------------------------------------------------------------------------- #
def test_a_crawl_the_script_called_simulated_stays_simulated(tool, monkeypatch) -> None:
    _stub_node(monkeypatch, _script_said(simulated=True, browser="http (no browser)"))

    result = tool._run(base_url="http://app.test", paths=["/"], max_pages=3, screenshots=False)

    assert result.ok
    assert result.data["simulated"] is True
    assert result.data["browser"] == "http (no browser)"


def test_a_real_browser_crawl_is_reported_as_one(tool, monkeypatch) -> None:
    _stub_node(monkeypatch, _script_said(simulated=False, browser="system chrome"))

    result = tool._run(base_url="http://app.test", paths=["/"], max_pages=3, screenshots=False)

    assert result.ok
    assert result.data["simulated"] is False
    # Which browser did the work is worth saying: "system chrome" and "bundled
    # chromium" are different enough that a locator behaving oddly is worth
    # tracing to one of them.
    assert result.data["browser"] == "system chrome"


def test_falling_back_to_http_says_why(tool, monkeypatch) -> None:
    """"Unverified" is useful. "Unverified and nobody knows why" is an outage.

    The browser path's error used to be dropped on the floor, so a run that
    degraded to static HTML left a `simulated` flag no one could explain.
    """
    monkeypatch.setattr(
        pw_tools.RunCommandTool, "run",
        lambda self, **_kwargs: ToolResult.success({"stdout": "no sentinel here"}),
    )
    monkeypatch.setattr(
        ExploreAppTool, "_explore_with_http",
        lambda self, *_a, **_k: ToolResult.success(
            {"base_url": "http://app.test", "snapshots": [], "errors": [], "simulated": True}
        ),
    )

    result = tool._run(base_url="http://app.test", paths=["/"], max_pages=3, screenshots=False)

    assert result.data["simulated"] is True
    assert result.data["errors"], "the fallback must carry a reason"
    assert "no browser crawl" in result.data["errors"][0]


def test_a_missing_toolchain_is_named_rather_than_guessed_at(tool, monkeypatch) -> None:
    monkeypatch.setattr(
        pw_tools, "probe_toolchain",
        lambda _root: {"node": True, "playwright_installed": False},
    )
    monkeypatch.setattr(
        ExploreAppTool, "_explore_with_http",
        lambda self, *_a, **_k: ToolResult.success(
            {"base_url": "http://app.test", "snapshots": [], "errors": [], "simulated": True}
        ),
    )

    result = tool._run(base_url="http://app.test", paths=["/"], max_pages=3, screenshots=False)

    assert "Playwright is not installed" in result.data["errors"][0]
