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
