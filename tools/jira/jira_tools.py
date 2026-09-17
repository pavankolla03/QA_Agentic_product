"""Jira integration — requirements in, defects out.

A QA engineer typing "automate PROJ-1234" should get the same quality of test
design as one who pastes the acceptance criteria by hand, so this tool
normalises a Jira issue into the same shape the Requirement Agent expects.
"""

from __future__ import annotations

import base64
import re
from typing import Any

import httpx

from configs.settings import get_settings
from packages.aiqa_types.enums import ToolCategory
from tools.base import Tool, ToolResult

ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,9}-\d+)\b")


def _auth_header() -> dict[str, str]:
    s = get_settings()
    if not (s.jira_email and s.jira_api_token):
        return {}
    token = base64.b64encode(f"{s.jira_email}:{s.jira_api_token}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _adf_to_text(node: Any) -> str:
    """Flatten Atlassian Document Format into plain text."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(_adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text", ""))
    parts = [_adf_to_text(child) for child in node.get("content", []) or []]
    joiner = "\n" if node.get("type") in ("paragraph", "listItem", "heading", "doc") else ""
    return joiner.join(p for p in parts if p)


class JiraFetchIssueTool(Tool):
    name = "jira.fetch_issue"
    category = ToolCategory.JIRA
    description = "Fetch a Jira issue and normalise its description/acceptance criteria into requirement text."
    schema = {"type": "object", "properties": {"issue_key": {"type": "string"}}, "required": ["issue_key"]}

    def _run(self, issue_key: str, **_: Any) -> ToolResult:
        s = get_settings()
        if not s.jira_base_url:
            return ToolResult.failure("Jira is not configured (set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN)")
        headers = _auth_header()
        if not headers:
            return ToolResult.failure("Jira credentials are missing (JIRA_EMAIL / JIRA_API_TOKEN)")

        key = issue_key.strip().upper()
        if not ISSUE_KEY_RE.fullmatch(key):
            return ToolResult.failure(f"'{issue_key}' is not a valid Jira issue key")

        url = f"{s.jira_base_url.rstrip('/')}/rest/api/3/issue/{key}"
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                response = client.get(url, headers={**headers, "Accept": "application/json"})
        except httpx.HTTPError as exc:
            return ToolResult.failure(f"Jira request failed: {exc}")
        if response.status_code == 404:
            return ToolResult.failure(f"Jira issue {key} not found")
        if response.status_code >= 400:
            return ToolResult.failure(f"Jira returned HTTP {response.status_code}")

        fields = (response.json() or {}).get("fields", {}) or {}
        description = _adf_to_text(fields.get("description"))

        # Acceptance criteria commonly live in a custom field; find it by name.
        criteria_text = ""
        for name, value in fields.items():
            if name.startswith("customfield") and isinstance(value, (str, dict, list)):
                flattened = _adf_to_text(value)
                if flattened and re.search(r"(?i)given|when|then|acceptance", flattened):
                    criteria_text = flattened
                    break

        return ToolResult.success(
            {
                "key": key,
                "summary": fields.get("summary", ""),
                "issue_type": ((fields.get("issuetype") or {}).get("name", "")),
                "status": ((fields.get("status") or {}).get("name", "")),
                "priority": ((fields.get("priority") or {}).get("name", "")),
                "labels": fields.get("labels", []),
                "components": [c.get("name") for c in (fields.get("components") or [])],
                "description": description[:20000],
                "acceptance_criteria": criteria_text[:8000],
                "requirement_text": "\n\n".join(
                    p for p in [fields.get("summary", ""), description, criteria_text] if p
                )[:24000],
                "url": f"{s.jira_base_url.rstrip('/')}/browse/{key}",
            }
        )


class JiraFetchEpicTool(Tool):
    """Every issue under an epic, as requirement text.

    Automating a feature one ticket at a time is how a backlog stays ahead of
    the QA team forever. This is the input to a batch run: one epic in, a
    prioritised list of requirements out.

    It deliberately does NOT start anything. A 30-issue epic is 30 runs, and
    that decision — with its cost — belongs to a human looking at the list.
    """

    name = "jira.fetch_epic"
    category = ToolCategory.JIRA
    description = "Fetch every child issue of a Jira epic as requirement text."
    schema = {
        "type": "object",
        "properties": {
            "epic_key": {"type": "string"},
            "max_issues": {"type": "integer", "minimum": 1, "maximum": 100},
            "statuses": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["epic_key"],
    }

    def _run(
        self,
        epic_key: str,
        max_issues: int = 50,
        statuses: list[str] | None = None,
        **_: Any,
    ) -> ToolResult:
        s = get_settings()
        if not s.jira_base_url:
            return ToolResult.failure("Jira is not configured (set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN)")
        headers = _auth_header()
        if not headers:
            return ToolResult.failure("Jira credentials are missing (JIRA_EMAIL / JIRA_API_TOKEN)")

        key = epic_key.strip().upper()
        if not ISSUE_KEY_RE.fullmatch(key):
            return ToolResult.failure(f"'{epic_key}' is not a valid Jira issue key")

        # `parent` covers team-managed projects; `"Epic Link"` company-managed.
        # Asking for both in one query avoids caring which this project is.
        jql = f'(parent = {key} OR "Epic Link" = {key})'
        if statuses:
            quoted = ", ".join(f'"{status}"' for status in statuses if status)
            if quoted:
                jql += f" AND status IN ({quoted})"
        jql += " ORDER BY priority DESC, created ASC"

        url = f"{s.jira_base_url.rstrip('/')}/rest/api/3/search"
        try:
            with httpx.Client(timeout=45, follow_redirects=True) as client:
                response = client.get(
                    url,
                    headers={**headers, "Accept": "application/json"},
                    params={
                        "jql": jql,
                        "maxResults": max(1, min(int(max_issues), 100)),
                        "fields": "summary,description,issuetype,status,priority,labels,components",
                    },
                )
        except httpx.HTTPError as exc:
            return ToolResult.failure(f"Jira request failed: {exc}")
        if response.status_code >= 400:
            return ToolResult.failure(
                f"Jira returned HTTP {response.status_code} for the epic query. "
                "Check that the epic key exists and the account can see it."
            )

        payload = response.json() or {}
        issues: list[dict[str, Any]] = []
        for raw in payload.get("issues", []) or []:
            fields = raw.get("fields", {}) or {}
            summary = fields.get("summary", "")
            description = _adf_to_text(fields.get("description"))
            issues.append(
                {
                    "key": raw.get("key", ""),
                    "summary": summary,
                    "issue_type": ((fields.get("issuetype") or {}).get("name", "")),
                    "status": ((fields.get("status") or {}).get("name", "")),
                    "priority": ((fields.get("priority") or {}).get("name", "")),
                    "labels": fields.get("labels", []),
                    "components": [c.get("name") for c in (fields.get("components") or [])],
                    "requirement_text": "\n\n".join(p for p in [summary, description] if p)[:16000],
                    "url": f"{s.jira_base_url.rstrip('/')}/browse/{raw.get('key', '')}",
                }
            )

        return ToolResult.success(
            {
                "epic": key,
                "issue_count": len(issues),
                "total_available": int(payload.get("total", len(issues))),
                "issues": issues,
                "note": (
                    "Nothing has been started. Feed these into a batch plan to see the "
                    "cost before committing to it."
                ),
            }
        )


class JiraCreateDefectTool(Tool):
    """Raise a defect when the Failure Analysis Agent concludes the *product* is broken."""

    name = "jira.create_defect"
    category = ToolCategory.JIRA
    description = "Create a Jira bug for a confirmed product defect. Requires human approval."
    mutating = True
    schema = {
        "type": "object",
        "properties": {
            "project_key": {"type": "string"},
            "summary": {"type": "string"},
            "description": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["project_key", "summary"],
    }

    def _run(
        self,
        project_key: str,
        summary: str,
        description: str = "",
        labels: list[str] | None = None,
        issue_type: str = "Bug",
        **_: Any,
    ) -> ToolResult:
        s = get_settings()
        headers = _auth_header()
        if not (s.jira_base_url and headers):
            return ToolResult.failure("Jira is not configured")
        if self.ctx.dry_run:
            return ToolResult.success({"summary": summary, "project": project_key}, dry_run=True)

        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary[:250],
                "issuetype": {"name": issue_type},
                "labels": (labels or []) + ["qagentic"],
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": description[:30000] or summary}]}
                    ],
                },
            }
        }
        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(
                    f"{s.jira_base_url.rstrip('/')}/rest/api/3/issue",
                    headers={**headers, "Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            return ToolResult.failure(f"Jira request failed: {exc}")
        if response.status_code >= 400:
            return ToolResult.failure(f"Jira returned HTTP {response.status_code}: {response.text[:300]}")

        data = response.json()
        key = data.get("key", "")
        if self.ctx.tracker is not None:
            self.ctx.tracker.audit("project_update", f"jira:{key}", "allowed", f"defect raised: {summary[:200]}")
        return ToolResult.success({"key": key, "url": f"{s.jira_base_url.rstrip('/')}/browse/{key}"})


JIRA_TOOLS = [JiraFetchIssueTool, JiraFetchEpicTool, JiraCreateDefectTool]
