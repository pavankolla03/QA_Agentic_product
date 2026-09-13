"""The VS Code manifest must agree with the TypeScript.

VS Code resolves commands, views and settings by string id at runtime, so a
mismatch between `package.json` and the source is invisible until a user clicks
something and nothing happens. Nothing in `tsc` catches it, and nothing in the
Python suite touched the extension at all — which is how a command can be added
to the manifest and never registered.

These are cheap string checks, and they are the only thing standing between a
typo and a dead button.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

EXTENSION = Path(__file__).resolve().parents[2] / "apps" / "vscode-extension"
MANIFEST = EXTENSION / "package.json"
SOURCE = EXTENSION / "src"

#: Commands VS Code itself provides or that are contributed for menus only.
_EXTERNAL_COMMANDS: set[str] = set()


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sources() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in SOURCE.rglob("*.ts")}


def _registered(sources: dict[Path, str]) -> set[str]:
    found: set[str] = set()
    for text in sources.values():
        found |= set(re.findall(r"register\(\s*'([a-zA-Z0-9._]+)'", text))
        found |= set(re.findall(r"registerCommand\(\s*'([a-zA-Z0-9._]+)'", text))
    return {c for c in found if c.startswith("aiqa.")}


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def test_every_declared_command_is_registered(manifest, sources) -> None:
    """A command in the palette that no code handles is a dead menu entry."""
    declared = {c["command"] for c in manifest["contributes"]["commands"]}
    missing = declared - _registered(sources) - _EXTERNAL_COMMANDS
    assert not missing, f"declared in package.json but never registered: {sorted(missing)}"


def test_every_registered_command_is_declared(manifest, sources) -> None:
    """A command with no manifest entry cannot be reached from the palette."""
    declared = {c["command"] for c in manifest["contributes"]["commands"]}
    missing = _registered(sources) - declared
    assert not missing, f"registered in TypeScript but not declared: {sorted(missing)}"


def test_menu_entries_reference_real_commands(manifest) -> None:
    declared = {c["command"] for c in manifest["contributes"]["commands"]}
    for menu, entries in manifest["contributes"].get("menus", {}).items():
        for entry in entries:
            command = entry.get("command")
            if command and command.startswith("aiqa."):
                assert command in declared, f"{menu} references undeclared {command}"


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
def test_every_view_has_a_provider(manifest, sources) -> None:
    declared = {
        view["id"]
        for views in manifest["contributes"].get("views", {}).values()
        for view in views
    }
    bound: set[str] = set()
    for text in sources.values():
        bound |= set(re.findall(r"registerTreeDataProvider\(\s*'([a-zA-Z0-9._]+)'", text))
    missing = declared - bound
    assert not missing, f"views with no provider (they render empty): {sorted(missing)}"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_every_setting_read_is_declared(manifest, sources) -> None:
    """An undeclared setting silently reads its hard-coded default forever."""
    declared = {
        key.split(".", 1)[1]
        for key in manifest["contributes"]["configuration"]["properties"]
        if key.startswith("aiqa.")
    }
    read: set[str] = set()
    for text in sources.values():
        read |= set(re.findall(r"\.get<[^>]+>\(\s*'([a-zA-Z0-9_]+)'", text))
    missing = read - declared
    assert not missing, f"read from configuration but not declared: {sorted(missing)}"


def test_the_control_plane_can_be_started_from_the_extension(manifest, sources) -> None:
    """F1: onboarding must not require the user to open a terminal."""
    declared = {c["command"] for c in manifest["contributes"]["commands"]}
    assert "aiqa.startServer" in declared
    assert "aiqa.autoStartServer" in manifest["contributes"]["configuration"]["properties"]

    joined = "\n".join(sources.values())
    assert "ServerManager" in joined
    # The server must be brought up during activation, not only on demand.
    assert re.search(r"await ensureServer\(\)", joined), "activate() never ensures a server"


def test_a_server_the_extension_did_not_start_is_never_killed(sources) -> None:
    """Stopping a shared or manually started server would be destructive."""
    manager = next(text for path, text in sources.items() if path.name == "serverManager.ts")
    stop = manager[manager.index("  stop(): void {") :]
    stop = stop[: stop.index("\n  }")]
    assert "this.child" in stop and "if (!child" in stop, (
        "stop() must be a no-op unless this extension owns the process"
    )
