"""Mobile automation architecture.

The MVP scope is web-first, so this layer is deliberately thin: it establishes
the *contract* (capabilities, session, locator strategies) that the agents
already target, and reports honestly when no device/emulator is attached. When a
team adds an Appium grid, the Test Design and Code Generation agents need no
changes — only this module gains a real driver.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from packages.aiqa_types.enums import ToolCategory
from tools.base import Tool, ToolResult

# Locator strategies ranked the same way as the web standard: stable id first.
MOBILE_LOCATOR_PRIORITY = [
    "accessibility_id",
    "resource_id",          # Android
    "name",                 # iOS
    "predicate",            # iOS NSPredicate
    "ui_automator",         # Android UiSelector
    "class_chain",          # iOS
    "xpath",                # last resort
]

DEFAULT_CAPABILITIES = {
    "android": {
        "platformName": "Android",
        "appium:automationName": "UiAutomator2",
        "appium:newCommandTimeout": 120,
        "appium:autoGrantPermissions": True,
    },
    "ios": {
        "platformName": "iOS",
        "appium:automationName": "XCUITest",
        "appium:newCommandTimeout": 120,
    },
}


def appium_url() -> str:
    return os.environ.get("APPIUM_SERVER_URL", "http://127.0.0.1:4723")


class MobileCapabilitiesTool(Tool):
    """Produce a validated capability set for the project's mobile target."""

    name = "mobile.capabilities"
    category = ToolCategory.MOBILE
    description = "Build an Appium capability set for the configured mobile platform."
    schema = {
        "type": "object",
        "properties": {
            "platform": {"type": "string", "enum": ["android", "ios"]},
            "device_name": {"type": "string"},
            "app_path": {"type": "string"},
            "app_package": {"type": "string"},
            "bundle_id": {"type": "string"},
        },
    }

    def _run(
        self,
        platform: str = "android",
        device_name: str = "",
        app_path: str = "",
        app_package: str = "",
        bundle_id: str = "",
        platform_version: str = "",
        **_: Any,
    ) -> ToolResult:
        key = platform.lower()
        if key not in DEFAULT_CAPABILITIES:
            return ToolResult.failure(f"unsupported mobile platform '{platform}' (expected android or ios)")

        caps = dict(DEFAULT_CAPABILITIES[key])
        if device_name:
            caps["appium:deviceName"] = device_name
        if platform_version:
            caps["appium:platformVersion"] = platform_version
        if app_path:
            caps["appium:app"] = app_path
        if key == "android" and app_package:
            caps["appium:appPackage"] = app_package
        if key == "ios" and bundle_id:
            caps["appium:bundleId"] = bundle_id

        missing = [
            field
            for field, present in (
                ("deviceName", bool(device_name)),
                ("app or appPackage/bundleId", bool(app_path or app_package or bundle_id)),
            )
            if not present
        ]
        return ToolResult.success(
            {
                "platform": key,
                "capabilities": caps,
                "locator_priority": MOBILE_LOCATOR_PRIORITY,
                "server_url": appium_url(),
                "missing": missing,
                "ready": not missing,
            }
        )


class MobileSessionProbeTool(Tool):
    """Report whether a real Appium grid is reachable, without pretending otherwise."""

    name = "mobile.probe"
    category = ToolCategory.MOBILE
    description = "Check whether an Appium server and device/emulator are available."

    def _run(self, **_: Any) -> ToolResult:
        url = appium_url().rstrip("/")
        try:
            with httpx.Client(timeout=8) as client:
                status = client.get(f"{url}/status")
                sessions = client.get(f"{url}/sessions")
        except httpx.HTTPError as exc:
            return ToolResult.success(
                {
                    "available": False,
                    "server_url": url,
                    "reason": f"Appium server not reachable: {exc}",
                    "hint": "Start it with `npx appium` and attach a device or emulator.",
                }
            )

        ready = status.status_code == 200
        payload: dict[str, Any] = {"available": ready, "server_url": url}
        try:
            payload["status"] = status.json().get("value", {})
            payload["active_sessions"] = len(sessions.json().get("value", []) or [])
        except (json.JSONDecodeError, ValueError):
            payload["status"] = {}
        return ToolResult.success(payload)


class MobileTestScaffoldTool(Tool):
    """Emit a WebdriverIO + Appium page-object scaffold consistent with the web standard."""

    name = "mobile.scaffold"
    category = ToolCategory.MOBILE
    description = "Generate an Appium page-object scaffold for a mobile screen."
    schema = {
        "type": "object",
        "properties": {
            "screen_name": {"type": "string"},
            "platform": {"type": "string"},
            "elements": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["screen_name"],
    }

    def _run(self, screen_name: str, platform: str = "android", elements: list[dict[str, Any]] | None = None, **_: Any) -> ToolResult:
        class_name = "".join(part.capitalize() for part in screen_name.replace("-", " ").split()) + "Screen"
        elements = elements or []

        getters: list[str] = []
        for element in elements:
            prop = element.get("name", "element")
            safe = "".join(c for c in prop.title().replace(" ", "") if c.isalnum()) or "Element"
            accessibility_id = element.get("accessibility_id") or element.get("test_id") or prop
            getters.append(
                f"  private get {safe[0].lower()}{safe[1:]}() {{\n"
                f"    return $('~{accessibility_id}');\n"
                f"  }}"
            )

        actions = "\n".join(
            [
                "  async waitForDisplayed(): Promise<void> {",
                "    await this.root.waitForDisplayed({ timeout: 15000 });",
                "  }",
            ]
        )
        code = "\n".join(
            [
                f"// {class_name} — generated by AI QA Engineer (mobile scaffold, {platform})",
                "",
                f"export class {class_name} {{",
                "  private get root() {",
                f"    return $('~{screen_name.lower().replace(' ', '-')}-screen');",
                "  }",
                "",
                *getters,
                "",
                actions,
                "}",
                "",
            ]
        )
        return ToolResult.success(
            {
                "class_name": class_name,
                "file_name": f"{class_name}.ts",
                "code": code,
                "platform": platform,
                "note": "Mobile execution requires an Appium grid; the architecture is wired but not executed in MVP.",
            }
        )


MOBILE_TOOLS = [MobileCapabilitiesTool, MobileSessionProbeTool, MobileTestScaffoldTool]
