"""API testing tools.

Used by the Test Design and Execution agents to verify behaviour below the UI —
faster and far less brittle than driving a browser for data setup/verification.
Credentials come from environment variable *names*, never literal values.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from packages.aiqa_types.enums import ToolCategory
from packages.security.redaction import redact
from tools.base import Tool, ToolResult

_BLOCKED_HOST_FRAGMENTS = ("169.254.169.254", "metadata.google.internal", "metadata.goog")


def _resolve_secret_refs(headers: dict[str, str] | None) -> dict[str, str]:
    """Expand ``{{env:TOKEN_NAME}}`` markers from the environment.

    The agent only ever sees the marker; the real value is injected here, at the
    boundary, and is redacted out of every trace afterwards.
    """
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        text = str(value)
        if text.startswith("{{env:") and text.endswith("}}"):
            env_name = text[6:-2].strip()
            out[key] = os.environ.get(env_name, "")
        else:
            out[key] = text
    return out


class HttpRequestTool(Tool):
    name = "api.request"
    category = ToolCategory.API
    description = "Send an HTTP request to the application's API and return status, headers and body."
    schema = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]},
            "url": {"type": "string", "description": "Absolute URL or a path relative to the project's api_base_url"},
            "headers": {"type": "object", "description": "Use {{env:NAME}} to reference a secret by env-var name"},
            "json_body": {"type": "object"},
            "params": {"type": "object"},
            "timeout": {"type": "integer"},
            "expect_status": {"type": "integer"},
        },
        "required": ["url"],
    }

    def _run(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        data: Any = None,
        params: dict[str, Any] | None = None,
        timeout: int = 30,
        expect_status: int | None = None,
        **_: Any,
    ) -> ToolResult:
        base = self.ctx.metadata.get("api_base_url") or self.ctx.metadata.get("base_url") or ""
        target = url if url.startswith(("http://", "https://")) else urljoin(base.rstrip("/") + "/", url.lstrip("/"))
        if not target.startswith(("http://", "https://")):
            return ToolResult.failure(f"cannot resolve URL '{url}' — no api_base_url configured for this project")
        if any(fragment in target for fragment in _BLOCKED_HOST_FRAGMENTS):
            return ToolResult.failure("refusing to call a cloud instance-metadata endpoint", rule="api.metadata_block")

        started = time.perf_counter()
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, verify=False) as client:
                response = client.request(
                    method.upper(),
                    target,
                    headers=_resolve_secret_refs(headers),
                    json=json_body,
                    content=data if isinstance(data, (bytes, str)) else None,
                    params=params,
                )
        except httpx.HTTPError as exc:
            return ToolResult.failure(f"request failed: {exc}")

        latency = int((time.perf_counter() - started) * 1000)
        body_text = response.text[:20000]
        parsed: Any = None
        if "json" in response.headers.get("content-type", ""):
            try:
                parsed = response.json()
            except (json.JSONDecodeError, ValueError):
                parsed = None

        payload = {
            "status": response.status_code,
            "ok": response.is_success,
            "latency_ms": latency,
            "headers": {k: redact(v) for k, v in dict(response.headers).items()},
            "body": parsed if parsed is not None else redact(body_text),
            "url": str(response.url),
        }
        if expect_status is not None and response.status_code != expect_status:
            return ToolResult(
                ok=False,
                data=payload,
                error=f"expected HTTP {expect_status}, received {response.status_code}",
                meta={"status": response.status_code},
            )
        return ToolResult.success(payload, status=response.status_code, latency_ms=latency)


class ApiHealthTool(Tool):
    name = "api.health"
    category = ToolCategory.API
    description = "Probe whether the application under test is reachable before a run starts."
    schema = {"type": "object", "properties": {"url": {"type": "string"}}}

    def _run(self, url: str = "", **_: Any) -> ToolResult:
        target = url or self.ctx.metadata.get("base_url", "")
        if not target:
            return ToolResult.success({"reachable": False, "reason": "no base_url configured"})
        try:
            with httpx.Client(timeout=10, follow_redirects=True, verify=False) as client:
                response = client.get(target)
            return ToolResult.success(
                {"reachable": response.status_code < 500, "status": response.status_code, "url": target}
            )
        except httpx.HTTPError as exc:
            return ToolResult.success({"reachable": False, "reason": str(exc)[:200], "url": target})


class OpenApiImportTool(Tool):
    """Turn an OpenAPI/Swagger document into candidate API test targets."""

    name = "api.import_openapi"
    category = ToolCategory.API
    description = "Read an OpenAPI spec and list endpoints worth covering with API tests."
    schema = {"type": "object", "properties": {"url": {"type": "string"}, "path": {"type": "string"}}}

    def _run(self, url: str = "", path: str = "", **_: Any) -> ToolResult:
        spec: dict[str, Any] | None = None
        if path:
            from packages.security.guard import WorkspaceGuard

            resolved = WorkspaceGuard(self.ctx.project_root).resolve_read(path)
            if not resolved.exists():
                return ToolResult.failure(f"spec not found: {path}")
            text = resolved.read_text(encoding="utf-8", errors="replace")
            try:
                spec = json.loads(text)
            except json.JSONDecodeError:
                import yaml

                spec = yaml.safe_load(text)
        elif url:
            try:
                with httpx.Client(timeout=20, follow_redirects=True, verify=False) as client:
                    response = client.get(url)
                response.raise_for_status()
                spec = response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                return ToolResult.failure(f"could not fetch spec: {exc}")
        if not isinstance(spec, dict):
            return ToolResult.failure("spec is not a JSON/YAML object")

        endpoints: list[dict[str, Any]] = []
        for route, operations in (spec.get("paths") or {}).items():
            if not isinstance(operations, dict):
                continue
            for method, op in operations.items():
                if method.lower() not in ("get", "post", "put", "patch", "delete"):
                    continue
                op = op if isinstance(op, dict) else {}
                endpoints.append(
                    {
                        "method": method.upper(),
                        "path": route,
                        "operation_id": op.get("operationId", ""),
                        "summary": op.get("summary", ""),
                        "tags": op.get("tags", []),
                        "required_params": [
                            p.get("name")
                            for p in (op.get("parameters") or [])
                            if isinstance(p, dict) and p.get("required")
                        ],
                        "success_codes": [c for c in (op.get("responses") or {}) if str(c).startswith("2")],
                    }
                )
        return ToolResult.success(
            {"title": (spec.get("info") or {}).get("title", ""), "endpoints": endpoints},
            count=len(endpoints),
        )


API_TOOLS = [HttpRequestTool, ApiHealthTool, OpenApiImportTool]
