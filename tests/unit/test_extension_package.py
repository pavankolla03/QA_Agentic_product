"""The packaged extension must be able to load.

A VSIX built with `--no-dependencies` shipped without `ws`, which the API
client imports at module scope. The extension then failed to load entirely:
no activation, no commands, and a bare "command 'aiqa.openChat' not found"
with nothing in the logs to explain it, because an extension that never loads
never logs.

Nothing in `tsc`, `pytest` or the manifest checks caught it — the code was
correct, the packaging was not. These tests check the artifact that actually
gets installed.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest

EXTENSION = Path(__file__).resolve().parents[2] / "apps" / "vscode-extension"
MANIFEST = EXTENSION / "package.json"
VSIX = EXTENSION / "ai-qa-engineer-0.1.0.vsix"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def vsix_names() -> list[str]:
    if not VSIX.exists():
        pytest.skip(f"{VSIX.name} has not been built")
    with zipfile.ZipFile(VSIX) as archive:
        return archive.namelist()


def _runtime_imports() -> set[str]:
    """Bare module specifiers imported by the extension's own TypeScript."""
    found: set[str] = set()
    pattern = re.compile(r"""^\s*import\s+[^;]*?from\s+['"]([^'"]+)['"]""", re.MULTILINE)
    for source in (EXTENSION / "src").rglob("*.ts"):
        for specifier in pattern.findall(source.read_text(encoding="utf-8")):
            # Relative paths are compiled in; `vscode` is provided by the host.
            if not specifier.startswith(".") and specifier != "vscode":
                found.add(specifier.split("/")[0])
    return found


def test_every_imported_package_is_declared_a_dependency(manifest) -> None:
    declared = set((manifest.get("dependencies") or {}) | (manifest.get("devDependencies") or {}))
    # Node built-ins need no declaration.
    builtins = {"path", "fs", "os", "child_process", "http", "https", "url", "crypto", "util", "events"}
    missing = _runtime_imports() - declared - builtins
    assert not missing, f"imported but not declared: {sorted(missing)}"


def test_every_runtime_dependency_is_bundled_in_the_vsix(manifest, vsix_names) -> None:
    """The bug that made this file necessary.

    `vsce package --no-dependencies` produces a VSIX that installs cleanly and
    then cannot load. The only place this is visible is the archive itself.
    """
    for package in (manifest.get("dependencies") or {}):
        prefix = f"extension/node_modules/{package}/"
        assert any(name.startswith(prefix) for name in vsix_names), (
            f"'{package}' is a runtime dependency but is not in the VSIX — "
            "the extension will fail to load. Package without --no-dependencies."
        )


def test_the_entry_point_is_present(manifest, vsix_names) -> None:
    main = str(manifest.get("main", "")).lstrip("./")
    assert main, "no main entry declared"
    assert f"extension/{main}" in vsix_names, f"{main} is missing from the VSIX"


def test_the_webview_assets_are_present(vsix_names) -> None:
    """The chat panel renders from these; without them it loads blank."""
    for asset in ("media/chat.js", "media/chat.css"):
        assert f"extension/{asset}" in vsix_names, f"{asset} is missing from the VSIX"


def test_activation_is_declared(manifest) -> None:
    events = manifest.get("activationEvents") or []
    assert events, "without an activation event no command is ever registered"


# --------------------------------------------------------------------------- #
# The compile gate
# --------------------------------------------------------------------------- #
def test_a_skipped_compile_check_is_not_reported_as_a_pass() -> None:
    """The most consequential bug in this platform's history.

    `tsc --noEmit` needs files on disk; the standards agent runs before they are
    written, so the check was skipped every time. A skipped checker contributes
    no errors, so `error_count == 0` read as "passed" — and every run reported
    standards green having compiled nothing. Four defects TypeScript would have
    caught in a second shipped that way.
    """
    from services.execution_service.static_validation import CheckOutcome, StaticReport

    unchecked = StaticReport(
        outcomes=[
            CheckOutcome("structure", ran=True, passed=True),
            CheckOutcome("typescript", ran=False, skipped_reason="not run before the files exist"),
        ]
    )
    assert unchecked.error_count == 0
    assert unchecked.compiled is False
    assert unchecked.verdict == "unverified", "a skip must never read as a pass"
    assert "NOT compile-checked" in unchecked.summary()

    checked = StaticReport(
        outcomes=[
            CheckOutcome("structure", ran=True, passed=True),
            CheckOutcome("typescript", ran=True, passed=True),
        ]
    )
    assert checked.verdict == "passed"
    assert "NOT compile-checked" not in checked.summary()


def test_the_compile_gate_runs_after_the_files_are_written() -> None:
    """It cannot run anywhere else: tsc needs the files to exist."""
    execution = (
        Path(__file__).resolve().parents[2] / "agents" / "execution" / "agent.py"
    ).read_text(encoding="utf-8")

    assert "_compile_check" in execution, "nothing re-checks after the diff is applied"
    applied = execution.index('ctx.metadata["changes_applied"] = True')
    checked = execution.index("self._compile_check(ctx, bundle)")
    assert applied < checked, "the check must come after the files land"


# --------------------------------------------------------------------------- #
# The sidebar
# --------------------------------------------------------------------------- #
def test_the_chat_has_a_container_to_itself(manifest) -> None:
    """Why the chat was invisible.

    Thirteen views shared one container and twelve of them defaulted to
    expanded, so the chat was a two-line strip above a stack of trees. It was
    contributed, registered and resolved -- and unusable. Copilot and Claude
    Code each give their chat a container of its own, and that is the whole
    difference between a chat you land on and one you scroll to.
    """
    views = manifest["contributes"]["views"]
    assert list(views["aiqa"]) and len(views["aiqa"]) == 1, (
        "the chat container must hold only the chat; anything else squeezes it"
    )
    assert views["aiqa"][0]["id"] == "aiqa.chatView"

    containers = {c["id"] for c in manifest["contributes"]["viewsContainers"]["activitybar"]}
    assert "aiqaWorkbench" in containers, "the trees need somewhere else to live"

    # Everything that is reference material starts collapsed; only work that
    # needs a decision is open.
    opens = {v["id"] for v in views["aiqaWorkbench"] if v.get("visibility") != "collapsed"}
    assert opens <= {"aiqa.approvals", "aiqa.runs"}, f"too much is open by default: {sorted(opens)}"


def test_every_container_icon_exists(manifest) -> None:
    for container in manifest["contributes"]["viewsContainers"]["activitybar"]:
        assert (EXTENSION / container["icon"]).is_file(), f"{container['icon']} is missing"


def test_the_chat_is_reachable_without_the_sidebar(manifest) -> None:
    """From any file: a keybinding and an editor-title button."""
    keys = {k["command"] for k in manifest["contributes"].get("keybindings", [])}
    assert "aiqa.openChat" in keys
    editor_title = manifest["contributes"]["menus"].get("editor/title", [])
    assert any(item["command"] == "aiqa.openChat" for item in editor_title)


def test_the_chat_renders_every_event_the_backend_emits() -> None:
    """`compile_checked` was emitted by the backend and dropped by the webview.

    The chat's switch had no case for it and a silent `default`, so the one
    line that says whether the generated code compiles was produced, streamed,
    received -- and never shown. Any future event type would have gone the same
    way, so this pins the contract from both ends.
    """
    import re

    root = Path(__file__).resolve().parents[2]
    emitted: set[str] = set()
    pattern = re.compile(
        r'(?:emit|emit_event|publish|record_event|add_event)\s*\(\s*[^)]*?["\']([a-z][a-z0-9_]+)["\']',
        re.S,
    )
    for source in root.rglob("*.py"):
        if any(part in source.parts for part in (".venv", "node_modules", ".git", "tests")):
            continue
        emitted |= set(pattern.findall(source.read_text(encoding="utf-8", errors="ignore")))

    chat_js = (EXTENSION / "media" / "chat.js").read_text(encoding="utf-8")
    handled = set(re.findall(r"case '([a-z_]+)':", chat_js))

    missing = emitted - handled
    assert not missing, f"the chat silently drops these emitted events: {sorted(missing)}"
