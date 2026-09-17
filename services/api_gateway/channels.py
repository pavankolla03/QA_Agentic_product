"""HTTP in front of the gateway: one route per channel, and one generic one.

Every route here does the same three things — parse with that channel's adapter,
hand the envelope to the gateway, render the reply with that channel's renderer
— so the routes are almost content-free on purpose. The moment one of them
starts making a decision the others do not, the platform has two behaviours and
no way to tell which a user got.

Authentication is this platform's own API key, on every channel, including the
webhook ones. A Slack body says which Slack user is speaking; it does not say
whether that person may start a run against a repository, and treating an
unauthenticated webhook as authorisation would mean anyone who learns the URL
can write files into somebody's working tree. Whoever wires the webhook up
supplies the key, exactly as the VS Code extension does.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from services.api_gateway.auth import Principal, requires
from services.gateway.adapters import ADAPTERS
from services.gateway.envelope import Channel, CommandEnvelope
from services.gateway.gateway import Gateway
from services.gateway.sessions import SessionStore

log = logging.getLogger("aiqa.channels")

channels = APIRouter(prefix="/api/channels", tags=["channels"])

_sessions = SessionStore()


def _gateway(request: Request) -> Gateway:
    engine = getattr(request.app.state, "engine", None)
    if engine is None:  # pragma: no cover - only before startup completes
        from services.agent_engine.engine import get_engine

        engine = get_engine()
    return Gateway(engine, sessions=_sessions)


async def _body(request: Request) -> dict[str, Any]:
    """Whatever this provider sent, as a dict.

    Slack slash commands are form-encoded and Twilio posts forms too, while the
    Events API and the Bot Framework send JSON. Accepting only JSON would reject
    half of the integrations this is for, with a 422 that says nothing useful.
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid JSON body: {exc}") from exc
        return payload if isinstance(payload, dict) else {"text": str(payload)}
    form = await request.form()
    return {key: value for key, value in form.items()}


@channels.post("/{channel}")
async def receive(
    channel: str,
    request: Request,
    principal: Principal = requires("run:write"),
) -> Any:
    """One command from one channel.

    `run:write` rather than `run:read`, because this route can start work. The
    chat endpoint only classifies and is read-only; this one creates runs.
    """
    try:
        which = Channel(channel)
    except ValueError:
        known = ", ".join(sorted(c.value for c in ADAPTERS))
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"unknown channel '{channel}'. Known channels: {known}"
        ) from None

    adapter = ADAPTERS.get(which)
    if adapter is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"'{channel}' has no webhook adapter; it talks to the platform directly",
        )

    parse, render = adapter
    payload = await _body(request)

    # Slack will not enable an integration whose endpoint cannot answer this,
    # and it arrives before any message ever does.
    if which is Channel.SLACK and payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    try:
        envelope = parse(payload)
    except Exception as exc:  # noqa: BLE001 - a malformed webhook is a 400, not a 500
        log.warning("could not parse a %s payload", channel, exc_info=True)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"could not read this {channel} payload: {exc}"
        ) from exc

    envelope.org_id = principal.org_id
    reply = await _gateway(request).handle(envelope)
    return render(reply)


@channels.post("")
async def receive_generic(
    request: Request,
    principal: Principal = requires("run:write"),
) -> dict[str, Any]:
    """The channel-agnostic form, for anything without a dedicated adapter.

    Takes the envelope's own fields rather than a provider's payload. This is
    what a script, a bespoke integration or a test uses, and it is the honest
    demonstration that the gateway needs nothing from a channel beyond text, a
    sender and a conversation.
    """
    payload = await _body(request)
    text = str(payload.get("text") or "")
    if not text.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "text is required")

    try:
        which = Channel(str(payload.get("channel") or Channel.API.value))
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown channel '{payload.get('channel')}'",
        ) from None

    envelope = CommandEnvelope(
        channel=which,
        text=text,
        external_user_id=str(payload.get("user") or principal.user_id),
        conversation_id=str(payload.get("conversation") or ""),
        project_id=str(payload.get("project_id") or ""),
        org_id=principal.org_id,
        metadata=dict(payload.get("metadata") or {}),
    )
    reply = await _gateway(request).handle(envelope)
    return {
        "kind": reply.kind,
        "text": reply.text,
        "run_id": reply.run_id,
        "suggestions": reply.suggestions,
        "target_url": reply.target_url,
        "autopilot": reply.autopilot,
        "error": reply.error,
    }


@channels.get("")
async def list_channels(_principal: Principal = requires("run:read")) -> dict[str, Any]:
    """What this installation can be reached through, and how."""
    return {
        "generic": {"path": "/api/channels", "method": "POST"},
        "webhooks": {
            which.value: {
                "path": f"/api/channels/{which.value}",
                "conversational": which.is_conversational,
                "voice": which.is_voice,
            }
            for which in ADAPTERS
        },
        "authentication": "X-API-Key, on every channel including webhooks",
    }
