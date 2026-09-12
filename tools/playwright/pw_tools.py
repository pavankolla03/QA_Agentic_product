"""Playwright tools: live application exploration and test execution.

Both degrade gracefully. If the project has no Playwright installation, the
explorer falls back to an HTTP + HTML probe (good enough for server-rendered
apps) and the runner reports a clear, actionable reason rather than failing the
whole run.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from packages.aiqa_types.enums import ToolCategory
from packages.aiqa_types.models import (
    DiscoveredElement,
    ExecutionResult,
    PageSnapshot,
    TestCaseResult,
)
from packages.security.guard import WorkspaceGuard
from tools.base import Tool, ToolResult
from tools.playwright.explorer_script import BEGIN, END, EXPLORER_MJS
from tools.shell.shell_tools import RunCommandTool, probe_toolchain


# =========================================================================== #
# Exploration
# =========================================================================== #
class _FormHTMLParser(HTMLParser):
    """Minimal fallback DOM probe for when no browser is available."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[dict[str, Any]] = []
        self.links: list[str] = []
        self.forms: list[dict[str, Any]] = []
        self.title = ""
        self._in_title = False
        self._pending_button: dict[str, Any] | None = None
        self._labels: dict[str, str] = {}
        self._current_label_for: str | None = None
        self._label_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "label":
            self._current_label_for = a.get("for")
            self._label_text = []
        elif tag == "a" and a.get("href"):
            href = a["href"]
            if not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self.links.append(href)
        elif tag == "form":
            self.forms.append(
                {
                    "index": len(self.forms),
                    "id": a.get("id") or None,
                    "name": a.get("name") or None,
                    "action": a.get("action") or None,
                    "method": (a.get("method") or "get").lower(),
                    "field_count": 0,
                    "submit_text": None,
                }
            )
        elif tag in ("input", "select", "textarea", "button"):
            if self.forms:
                self.forms[-1]["field_count"] += 1
            self.elements.append(
                {
                    "tag": tag,
                    "id": a.get("id") or "",
                    "name": a.get("name") or "",
                    "test_id": a.get("data-testid") or a.get("data-test-id") or a.get("data-cy") or None,
                    "placeholder": a.get("placeholder") or None,
                    "input_type": a.get("type") or ("text" if tag == "input" else None),
                    "required": "required" in a,
                    "aria_label": a.get("aria-label") or "",
                    "value": a.get("value") or "",
                }
            )
            if tag == "button":
                self._pending_button = self.elements[-1]

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "label":
            if self._current_label_for:
                self._labels[self._current_label_for] = "".join(self._label_text).strip()
            self._current_label_for = None
        elif tag == "button":
            self._pending_button = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data.strip()
        if self._current_label_for is not None:
            self._label_text.append(data)
        if self._pending_button is not None and data.strip():
            self._pending_button["text"] = data.strip()[:80]

    def to_elements(self) -> list[DiscoveredElement]:
        out: list[DiscoveredElement] = []
        for raw in self.elements:
            label = self._labels.get(raw["id"], "") or raw["aria_label"]
            text = raw.get("text") or raw.get("value") or ""
            tag = raw["tag"]
            itype = (raw.get("input_type") or "").lower()
            role = (
                "button"
                if tag == "button" or itype in ("submit", "button", "reset")
                else "checkbox" if itype == "checkbox"
                else "radio" if itype == "radio"
                else "combobox" if tag == "select"
                else "textbox"
            )
            name = label or text or raw["name"] or raw["placeholder"] or ""

            if raw["test_id"]:
                locator, strategy, confidence = f"getByTestId('{raw['test_id']}')", "getByTestId", 0.98
            elif name:
                locator, strategy, confidence = (
                    f"getByRole('{role}', {{ name: '{name}' }})", "getByRole", 0.8,
                )
            elif raw["placeholder"]:
                locator, strategy, confidence = f"getByPlaceholder('{raw['placeholder']}')", "getByPlaceholder", 0.7
            elif raw["id"]:
                locator, strategy, confidence = f"locator('#{raw['id']}')", "css", 0.5
            elif raw["name"]:
                locator, strategy, confidence = f"locator('[name=\"{raw['name']}\"]')", "css", 0.45
            else:
                locator, strategy, confidence = "", "", 0.2

            out.append(
                DiscoveredElement(
                    role=role, name=name, tag=tag, test_id=raw["test_id"], label=label or None,
                    placeholder=raw["placeholder"], text=text or None, input_type=raw.get("input_type"),
                    required=raw["required"], recommended_locator=locator,
                    locator_strategy=strategy, confidence=confidence,
                )
            )
        return out


class ExploreAppTool(Tool):
    """Drive a real browser over the application under test and harvest locators."""

    name = "playwright.explore"
    category = ToolCategory.PLAYWRIGHT
    description = "Crawl the application under test and capture elements, forms and stable locators."
    schema = {
        "type": "object",
        "properties": {
            "base_url": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"}},
            "max_pages": {"type": "integer"},
            "timeout": {"type": "integer"},
        },
        "required": ["base_url"],
    }

    def _run(
        self,
        base_url: str,
        paths: list[str] | None = None,
        max_pages: int = 5,
        timeout: int = 20000,
        screenshots: bool = True,
        **_: Any,
    ) -> ToolResult:
        if not base_url:
            return ToolResult.failure("no base_url configured for this project — set it to enable exploration")

        toolchain = probe_toolchain(self.ctx.project_root)
        if toolchain["node"] and toolchain["playwright_installed"]:
            result = self._explore_with_playwright(base_url, paths or ["/"], max_pages, timeout, screenshots)
            if result.ok:
                return result
            # Fall through to the HTTP probe when the browser run fails.
        return self._explore_with_http(base_url, paths or ["/"], max_pages)

    # -- real browser --------------------------------------------------- #
    def _explore_with_playwright(
        self, base_url: str, paths: list[str], max_pages: int, timeout: int, screenshots: bool
    ) -> ToolResult:
        root = Path(self.ctx.project_root)
        aiqa_dir = root / ".aiqa"
        aiqa_dir.mkdir(parents=True, exist_ok=True)
        script_path = aiqa_dir / "explore.mjs"
        script_path.write_text(EXPLORER_MJS, encoding="utf-8", newline="\n")

        shot_dir = aiqa_dir / "screenshots"
        if screenshots:
            shot_dir.mkdir(parents=True, exist_ok=True)

        config = {
            "baseUrl": base_url,
            "paths": paths,
            "maxPages": max_pages,
            "timeout": timeout,
            "screenshotDir": shot_dir.as_posix() if screenshots else None,
        }
        runner = RunCommandTool(self.ctx)
        result = runner.run(
            command=["node", str(script_path), json.dumps(config)],
            cwd=".",
            timeout=max(120, (timeout // 1000) * max_pages + 60),
        )
        payload = (result.data or {}) if isinstance(result.data, dict) else {}
        stdout = payload.get("stdout", "")
        if BEGIN not in stdout or END not in stdout:
            return ToolResult.failure(
                f"browser exploration produced no result ({result.error or 'no sentinel in output'})"
            )

        raw = stdout.split(BEGIN, 1)[1].split(END, 1)[0].strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return ToolResult.failure(f"could not parse exploration output: {exc}")

        snapshots = [
            PageSnapshot(
                url=snap.get("url", ""),
                title=snap.get("title", ""),
                route_pattern=_route_pattern(snap.get("url", "")),
                elements=[DiscoveredElement(**el) for el in snap.get("elements", [])],
                forms=snap.get("forms", []),
                navigations=snap.get("navigations", []),
                screenshot_path=snap.get("screenshot_path"),
            )
            for snap in data.get("snapshots", [])
        ]
        return ToolResult.success(
            {
                "base_url": base_url,
                "snapshots": [s.model_dump(mode="json") for s in snapshots],
                "unreachable": data.get("unreachable", []),
                "errors": data.get("errors", []),
                "simulated": False,
            },
            pages=len(snapshots),
            elements=sum(len(s.elements) for s in snapshots),
        )

    # -- HTTP fallback -------------------------------------------------- #
    def _explore_with_http(self, base_url: str, paths: list[str], max_pages: int) -> ToolResult:
        import httpx

        snapshots: list[PageSnapshot] = []
        unreachable: list[str] = []
        errors: list[str] = []
        queue = list(paths)
        seen: set[str] = set()
        origin = urlparse(base_url)

        try:
            with httpx.Client(timeout=15.0, follow_redirects=True, verify=False) as client:
                while queue and len(snapshots) < max_pages:
                    path = queue.pop(0)
                    url = path if path.startswith("http") else urljoin(base_url, path)
                    if url in seen:
                        continue
                    seen.add(url)
                    try:
                        resp = client.get(url)
                    except httpx.HTTPError as exc:
                        unreachable.append(url)
                        errors.append(f"{url}: {exc}")
                        continue
                    if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", ""):
                        unreachable.append(url)
                        continue

                    parser = _FormHTMLParser()
                    parser.feed(resp.text)
                    snapshots.append(
                        PageSnapshot(
                            url=url,
                            title=parser.title,
                            route_pattern=_route_pattern(url),
                            elements=parser.to_elements(),
                            forms=parser.forms,
                            navigations=parser.links[:40],
                        )
                    )
                    for href in parser.links:
                        try:
                            nxt = urljoin(url, href)
                            if urlparse(nxt).netloc == origin.netloc and nxt not in seen:
                                queue.append(nxt)
                        except ValueError:
                            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

        if not snapshots:
            return ToolResult.failure(
                f"could not reach {base_url}. Install Playwright in the project "
                f"(`npm i -D @playwright/test && npx playwright install`) or verify the app is running. "
                f"Details: {'; '.join(errors[:2]) or 'no HTML responses'}"
            )
        return ToolResult.success(
            {
                "base_url": base_url,
                "snapshots": [s.model_dump(mode="json") for s in snapshots],
                "unreachable": unreachable,
                "errors": errors,
                "simulated": True,
            },
            pages=len(snapshots),
            elements=sum(len(s.elements) for s in snapshots),
            fallback="http",
        )


def _route_pattern(url: str) -> str:
    """Generalise `/residents/4821/edit` → `/residents/:id/edit`."""
    path = urlparse(url).path or "/"
    parts = []
    for segment in path.split("/"):
        if not segment:
            continue
        if segment.isdigit() or re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", segment):
            parts.append(":id")
        else:
            parts.append(segment)
    return "/" + "/".join(parts)


# =========================================================================== #
# Execution
# =========================================================================== #
class RunTestsTool(Tool):
    """Execute the project's Playwright suite and normalise the report."""

    name = "playwright.run_tests"
    category = ToolCategory.PLAYWRIGHT
    description = "Run Playwright tests and return structured per-test results."
    mutating = True
    schema = {
        "type": "object",
        "properties": {
            "test_filter": {"type": "string", "description": "grep pattern or file path"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "workers": {"type": "integer"},
            "retries": {"type": "integer"},
            "timeout": {"type": "integer"},
        },
    }

    def _run(
        self,
        test_filter: str = "",
        tags: list[str] | None = None,
        workers: int = 0,
        retries: int = 0,
        timeout: int = 900,
        **_: Any,
    ) -> ToolResult:
        root = Path(self.ctx.project_root)
        toolchain = probe_toolchain(str(root))

        if not toolchain["has_package_json"]:
            return ToolResult.failure("no package.json — this project does not look like a Node test repository")
        if not toolchain["playwright_installed"]:
            return ToolResult.failure(
                "Playwright is not installed in this project. Run `npm install` "
                "(and `npx playwright install` for browsers), then re-run."
            )

        report_path = root / ".aiqa" / "results.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        if report_path.exists():
            report_path.unlink()

        command = ["npx", "playwright", "test", "--reporter=json"]
        if test_filter:
            command.append(test_filter)
        if tags:
            command += ["--grep", "|".join(re.escape(t) for t in tags)]
        if workers:
            command += [f"--workers={workers}"]
        if retries:
            command += [f"--retries={retries}"]

        runner = RunCommandTool(self.ctx)
        result = runner.run(
            command=command,
            cwd=".",
            timeout=timeout,
            env={"PLAYWRIGHT_JSON_OUTPUT_NAME": str(report_path)},
        )
        payload = result.data if isinstance(result.data, dict) else {}
        stdout = payload.get("stdout", "")
        stderr = payload.get("stderr", "")
        exit_code = payload.get("exit_code", 1)

        raw_report: dict[str, Any] | None = None
        if report_path.exists():
            try:
                raw_report = json.loads(report_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw_report = None
        if raw_report is None:
            raw_report = _extract_json_object(stdout)

        if raw_report is None:
            return ToolResult.failure(
                f"Playwright produced no parseable report (exit {exit_code}). "
                f"{(stderr or stdout).strip()[:400]}"
            )

        execution = parse_playwright_report(
            raw_report,
            command=" ".join(command),
            cwd=str(root),
            exit_code=int(exit_code),
            stdout_tail=stdout[-4000:],
            stderr_tail=stderr[-2000:],
        )
        execution.report_path = str(report_path)
        # A non-zero exit with parsed failures is a *successful tool call* that
        # found failing tests — the agent, not the tool, decides what that means.
        return ToolResult.success(execution.model_dump(mode="json"),
                                  passed=execution.passed, failed=execution.failed, total=execution.total)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    while start != -1:
        depth, in_string, escape = 0, False, False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(text[start : idx + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(candidate, dict) and ("suites" in candidate or "stats" in candidate):
                        return candidate
                    break
        start = text.find("{", start + 1)
    return None


def parse_playwright_report(
    report: dict[str, Any],
    command: str = "",
    cwd: str = "",
    exit_code: int = 0,
    stdout_tail: str = "",
    stderr_tail: str = "",
) -> ExecutionResult:
    """Flatten Playwright's nested JSON reporter output into :class:`ExecutionResult`."""
    from packages.aiqa_types.enums import TestStatus

    results: list[TestCaseResult] = []

    def walk(suites: list[dict[str, Any]], file_path: str = "") -> None:
        for suite in suites or []:
            current_file = suite.get("file") or file_path
            for spec in suite.get("specs", []) or []:
                for test in spec.get("tests", []) or []:
                    attempts = test.get("results", []) or []
                    if not attempts:
                        continue
                    final = attempts[-1]
                    status_raw = (final.get("status") or "").lower()
                    expected = (test.get("expectedStatus") or "passed").lower()

                    if status_raw == "passed" and len(attempts) > 1:
                        status = TestStatus.FLAKY
                    elif status_raw == "passed":
                        status = TestStatus.PASSED
                    elif status_raw == "skipped":
                        status = TestStatus.SKIPPED
                    elif status_raw == "timedout":
                        status = TestStatus.TIMED_OUT
                    elif status_raw == "interrupted":
                        status = TestStatus.INTERRUPTED
                    elif status_raw == expected:
                        status = TestStatus.PASSED
                    else:
                        status = TestStatus.FAILED

                    error = final.get("error") or {}
                    message = _strip_ansi(str(error.get("message", "")))
                    stack = _strip_ansi(str(error.get("stack", "")))

                    attachments = {a.get("name"): a.get("path") for a in (final.get("attachments") or [])}
                    stdout_chunks = "".join(
                        c.get("text", "") for c in (final.get("stdout") or []) if isinstance(c, dict)
                    )

                    results.append(
                        TestCaseResult(
                            test_id=_extract_test_id(spec.get("title", "")),
                            name=spec.get("title", "") or test.get("title", ""),
                            file_path=current_file,
                            status=status,
                            duration_ms=int(final.get("duration", 0) or 0),
                            retries=max(0, len(attempts) - 1),
                            error_message=message[:4000],
                            error_stack=stack[:6000],
                            failed_locator=_extract_locator(message + "\n" + stack),
                            failed_step=_extract_failed_step(message),
                            screenshot_path=attachments.get("screenshot"),
                            video_path=attachments.get("video"),
                            trace_path=attachments.get("trace"),
                            stdout=stdout_chunks[:2000],
                            tags=[t for t in (spec.get("tags") or []) if isinstance(t, str)],
                        )
                    )
            walk(suite.get("suites", []) or [], current_file)

    walk(report.get("suites", []) or [])

    stats = report.get("stats", {}) or {}
    execution = ExecutionResult(
        command=command,
        cwd=cwd,
        exit_code=exit_code,
        duration_ms=int(stats.get("duration", 0) or 0),
        results=results,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )
    execution.total = len(results)
    execution.passed = sum(1 for r in results if r.status == TestStatus.PASSED)
    execution.failed = sum(1 for r in results if r.status in (TestStatus.FAILED, TestStatus.TIMED_OUT))
    execution.skipped = sum(1 for r in results if r.status == TestStatus.SKIPPED)
    execution.flaky = sum(1 for r in results if r.status == TestStatus.FLAKY)
    return execution


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TEST_ID_RE = re.compile(r"\b(TC-[A-Z0-9]+-\d+)\b")
_LOCATOR_RES = [
    re.compile(r"(getBy\w+\((?:[^()]|\([^()]*\))*\))"),
    re.compile(r"(locator\((?:[^()]|\([^()]*\))*\))"),
    re.compile(r"waiting for (?:locator\()?['\"]([^'\"]+)['\"]"),
]


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _extract_test_id(title: str) -> str:
    match = _TEST_ID_RE.search(title or "")
    return match.group(1) if match else ""


def _extract_locator(text: str) -> str:
    for pattern in _LOCATOR_RES:
        match = pattern.search(text or "")
        if match:
            return match.group(1)[:200]
    return ""


def _extract_failed_step(message: str) -> str:
    for line in (message or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("Given ", "When ", "Then ", "And ", "But ")):
            return stripped[:200]
    return ""


class InstallBrowsersTool(Tool):
    name = "playwright.install"
    category = ToolCategory.PLAYWRIGHT
    description = "Install Playwright browser binaries into the project."
    mutating = True

    def _run(self, browser: str = "chromium", **_: Any) -> ToolResult:
        runner = RunCommandTool(self.ctx)
        return runner.run(command=["npx", "playwright", "install", browser, "--with-deps"], cwd=".", timeout=900)


class CheckSetupTool(Tool):
    name = "playwright.check_setup"
    category = ToolCategory.PLAYWRIGHT
    description = "Report which parts of the Playwright toolchain are available."

    def _run(self, **_: Any) -> ToolResult:
        WorkspaceGuard(self.ctx.project_root)  # validates the root is usable
        return ToolResult.success(probe_toolchain(self.ctx.project_root))


PLAYWRIGHT_TOOLS = [ExploreAppTool, RunTestsTool, InstallBrowsersTool, CheckSetupTool]
