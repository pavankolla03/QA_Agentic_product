"""Slack and Microsoft Teams reporting.

Both are webhook-based so no bot install is required. Messages are redacted
before sending — a failure trace can easily contain a token, and a chat channel
is a permanent, widely-readable store.
"""

from __future__ import annotations

from typing import Any

import httpx

from configs.settings import get_settings
from packages.aiqa_types.enums import ToolCategory
from packages.security.redaction import redact
from tools.base import Tool, ToolResult

# Amber, not green, for `blocked`. The platform worked and produced nothing it
# can vouch for, and nobody scanning a channel opens the report to check a tick.
_STATUS_COLOR = {
    "succeeded": "#2eb886",
    "blocked": "#e8a33d",
    "failed": "#d63b3b",
    "partial": "#e8a33d",
    "cancelled": "#8a8a8a",
    "budget_exceeded": "#e8a33d",
}
_STATUS_EMOJI = {
    "succeeded": "✅",
    "blocked": "🚧",
    "failed": "❌",
    "partial": "⚠️",
    "cancelled": "🚫",
    "budget_exceeded": "💰",
}


def _summary_fields(payload: dict[str, Any]) -> list[tuple[str, str]]:
    total = payload.get("tests_total", 0)
    passed = payload.get("tests_passed", 0)
    failed = payload.get("tests_failed", 0)
    fields = [
        ("Project", str(payload.get("project", "—"))),
        ("Scenarios designed", str(payload.get("scenarios_designed", 0))),
        ("Files changed", str(payload.get("files_changed", 0))),
        ("Tests", f"{passed}/{total} passed" + (f", {failed} failed" if failed else "")),
        ("Self-heals applied", str(payload.get("heals_applied", 0))),
        ("Cost", f"${float(payload.get('cost_usd', 0.0)):.4f}"),
        ("Duration", f"{float(payload.get('duration_s', 0.0)):.1f}s"),
    ]
    return [(k, v) for k, v in fields if v not in ("", "None")]


class SlackNotifyTool(Tool):
    name = "slack.notify"
    category = ToolCategory.SLACK
    description = "Post a run summary to Slack via an incoming webhook."
    mutating = True
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "text": {"type": "string"},
            "status": {"type": "string"},
            "payload": {"type": "object"},
            "webhook_url": {"type": "string"},
        },
    }

    def _run(
        self,
        title: str = "QAgentic run",
        text: str = "",
        status: str = "succeeded",
        payload: dict[str, Any] | None = None,
        webhook_url: str = "",
        **_: Any,
    ) -> ToolResult:
        url = webhook_url or get_settings().slack_webhook_url
        if not url:
            return ToolResult.failure("Slack is not configured (set SLACK_WEBHOOK_URL)", rule="notify.unconfigured")

        payload = payload or {}
        emoji = _STATUS_EMOJI.get(status, "ℹ️")
        fields = _summary_fields(payload)

        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {title}"[:150]}},
        ]
        if text:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": redact(text)[:2900]}})
        if fields:
            blocks.append(
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*{key}*\n{redact(value)}"} for key, value in fields[:10]
                    ],
                }
            )
        failures = payload.get("failures") or []
        if failures:
            listed = "\n".join(f"• `{redact(str(f))[:140]}`" for f in failures[:6])
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Failing tests*\n{listed}"}})
        defects = payload.get("product_defects") or []
        if defects:
            listed = "\n".join(f"• {redact(str(d))[:160]}" for d in defects[:5])
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Suspected product defects*\n{listed}"}})
        if payload.get("run_url"):
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"<{payload['run_url']}|Open run in the control plane>"}],
                }
            )

        body = {
            "text": f"{emoji} {title}",
            "blocks": blocks,
            "attachments": [{"color": _STATUS_COLOR.get(status, "#4a90d9"), "blocks": []}],
        }

        if self.ctx.dry_run:
            return ToolResult.success(body, dry_run=True)
        try:
            with httpx.Client(timeout=20) as client:
                response = client.post(url, json=body)
        except httpx.HTTPError as exc:
            return ToolResult.failure(f"Slack webhook failed: {exc}")
        if response.status_code >= 400:
            return ToolResult.failure(f"Slack returned HTTP {response.status_code}: {response.text[:200]}")
        return ToolResult.success({"delivered": True, "blocks": len(blocks)})


class TeamsNotifyTool(Tool):
    name = "teams.notify"
    category = ToolCategory.TEAMS
    description = "Post a run summary to Microsoft Teams via an incoming webhook (Adaptive Card)."
    mutating = True

    def _run(
        self,
        title: str = "QAgentic run",
        text: str = "",
        status: str = "succeeded",
        payload: dict[str, Any] | None = None,
        webhook_url: str = "",
        **_: Any,
    ) -> ToolResult:
        url = webhook_url or get_settings().teams_webhook_url
        if not url:
            return ToolResult.failure("Teams is not configured (set TEAMS_WEBHOOK_URL)", rule="notify.unconfigured")

        payload = payload or {}
        emoji = _STATUS_EMOJI.get(status, "ℹ️")
        facts = [{"title": key, "value": redact(value)} for key, value in _summary_fields(payload)]

        card_body: list[dict[str, Any]] = [
            {"type": "TextBlock", "size": "Large", "weight": "Bolder", "text": f"{emoji} {title}"[:150], "wrap": True},
        ]
        if text:
            card_body.append({"type": "TextBlock", "text": redact(text)[:3000], "wrap": True})
        if facts:
            card_body.append({"type": "FactSet", "facts": facts})
        failures = payload.get("failures") or []
        if failures:
            card_body.append(
                {
                    "type": "TextBlock",
                    "weight": "Bolder",
                    "text": "Failing tests",
                    "wrap": True,
                    "spacing": "Medium",
                }
            )
            card_body.append(
                {"type": "TextBlock", "text": "\n\n".join(f"- {redact(str(f))[:140]}" for f in failures[:6]), "wrap": True}
            )

        actions: list[dict[str, Any]] = []
        if payload.get("run_url"):
            actions.append({"type": "Action.OpenUrl", "title": "Open run", "url": payload["run_url"]})

        body = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": card_body,
                        "actions": actions,
                    },
                }
            ],
        }

        if self.ctx.dry_run:
            return ToolResult.success(body, dry_run=True)
        try:
            with httpx.Client(timeout=20) as client:
                response = client.post(url, json=body)
        except httpx.HTTPError as exc:
            return ToolResult.failure(f"Teams webhook failed: {exc}")
        if response.status_code >= 400:
            return ToolResult.failure(f"Teams returned HTTP {response.status_code}: {response.text[:200]}")
        return ToolResult.success({"delivered": True})


NOTIFY_TOOLS = [SlackNotifyTool, TeamsNotifyTool]
