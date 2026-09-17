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
    """A view with no provider renders as an empty panel with no error."""
    declared = {
        view["id"]
        for views in manifest["contributes"].get("views", {}).values()
        for view in views
    }
    bound: set[str] = set()
    for text in sources.values():
        # Tree views bind by literal id; a webview view binds via the static
        # `viewType` on its provider class, so accept either spelling.
        bound |= set(re.findall(r"registerTreeDataProvider\(\s*'([a-zA-Z0-9._]+)'", text))
        bound |= set(re.findall(r"registerWebviewViewProvider\(\s*'([a-zA-Z0-9._]+)'", text))
        bound |= set(re.findall(r"viewType\s*=\s*'([a-zA-Z0-9._]+)'", text))
    missing = declared - bound
    assert not missing, f"views with no provider (they render empty): {sorted(missing)}"


def test_the_chat_is_a_docked_view_not_a_command_only_panel(manifest, sources) -> None:
    """Clicking the extension icon must land on somewhere to type.

    It used to open a tree of links, with the chat behind a command name you
    had to remember. Copilot and Claude Code dock the conversation instead,
    and the conversation is this product's primary surface too.
    """
    views = manifest["contributes"]["views"]["aiqa"]
    assert views[0]["id"] == "aiqa.chatView", "the chat must be the first view in the container"
    assert views[0].get("type") == "webview", "a tree cannot host a chat"

    joined = "\n".join(sources.values())
    assert "registerWebviewViewProvider" in joined
    # A run outlives a glance at another view; losing the transcript would make
    # the sidebar useless precisely when it matters.
    assert "retainContextWhenHidden" in joined


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


def test_the_extension_id_is_not_the_product_name(manifest: dict) -> None:
    """Renaming the product must not orphan everybody's installed extension.

    `publisher.name` is the extension's identity to VS Code, not a label. When
    the rename to QAgentic changed `name` too, installing the new build left the
    old one in place: two extensions contributing the same commands, the same
    keybindings and the same activity-bar containers, with VS Code free to pick
    either. The visible name is `displayName`, and that is the one that changed.

    This is the same rule the rest of the rename follows — `aiqa` stays as the
    namespace for the CLI, the environment variables and the .aiqa/ directory.
    """
    assert manifest["name"] == "ai-qa-engineer"
    assert manifest["publisher"] == "aiqa"
    assert manifest["displayName"] == "QAgentic"


def test_the_product_name_is_what_a_user_actually_sees(manifest: dict) -> None:
    """Every label VS Code renders says QAgentic, whatever the ids say."""
    containers = manifest["contributes"]["viewsContainers"]["activitybar"]
    assert [container["title"] for container in containers] == ["QAgentic", "QAgentic Workbench"]

    stale = [
        command["title"]
        for command in manifest["contributes"]["commands"]
        if "AI QA" in command["title"] or "AI QA" in command.get("category", "")
    ]
    assert stale == []

