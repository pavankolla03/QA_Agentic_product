"""Turning what a channel sent into an envelope, and a reply back into its shape.

Each adapter is deliberately small and deliberately stupid. It knows one
provider's payload format and nothing about what the message means — the moment
an adapter starts deciding whether something is a question or a request to
automate, there are two implementations of that decision and they begin to
drift.

Inbound payloads are untrusted. A Slack body names a user id, and that id says
who is speaking, never what they may do: authorisation is resolved from the
session and the API key that carried the request, not from a field somebody else
filled in.
"""

from __future__ import annotations

from typing import Any

from services.gateway.envelope import Channel, CommandEnvelope, Reply

#: Slack and Teams both cap a single text block well below this, and a reply
#: that arrives truncated mid-sentence reads as a broken integration.
_BLOCK_LIMIT = 2900
#: One SMS-shaped message. WhatsApp permits far more, but a wall of text on a
#: phone is not read.
_PHONE_LIMIT = 1200


def from_slack(payload: dict[str, Any]) -> CommandEnvelope:
    """A Slack Events API message, or a slash command.

    Both shapes appear: `event_callback` for messages and mentions, and a flat
    form-encoded body for `/qagentic ...`. They carry the same three things
    under different names.
    """
    event = payload.get("event") or {}
    text = str(event.get("text") or payload.get("text") or "")
    return CommandEnvelope(
        channel=Channel.SLACK,
        # A mention arrives as "<@U0123> automate https://..."; the mention is
        # addressing, not instruction, and leaving it in makes the message look
        # like it names a target.
        text=_strip_mentions(text),
        external_user_id=str(event.get("user") or payload.get("user_id") or ""),
        conversation_id=str(
            event.get("thread_ts") or event.get("channel") or payload.get("channel_id") or ""
        ),
        metadata={"team": payload.get("team_id", ""), "event_type": event.get("type", "")},
    )


def to_slack(reply: Reply) -> dict[str, Any]:
    """Slack's `blocks`, with the text as a fallback for clients that ignore them."""
    text = reply.text[:_BLOCK_LIMIT]
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text or "(no answer)"}}
    ]
    if reply.suggestions:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"_try:_ {' · '.join(reply.suggestions[:3])}"}
                ],
            }
        )
    return {"response_type": "in_channel", "text": text, "blocks": blocks}


def from_teams(payload: dict[str, Any]) -> CommandEnvelope:
    """A Bot Framework activity."""
    sender = payload.get("from") or {}
    conversation = payload.get("conversation") or {}
    return CommandEnvelope(
        channel=Channel.TEAMS,
        text=_strip_mentions(str(payload.get("text") or "")),
        external_user_id=str(sender.get("id") or ""),
        conversation_id=str(conversation.get("id") or ""),
        metadata={"activity_id": payload.get("id", ""), "locale": payload.get("locale", "")},
    )


def to_teams(reply: Reply) -> dict[str, Any]:
    return {"type": "message", "text": reply.text[:_BLOCK_LIMIT] or "(no answer)"}


def from_whatsapp(payload: dict[str, Any]) -> CommandEnvelope:
    """A WhatsApp Cloud API webhook, or a Twilio form post.

    The Cloud API buries the message several levels down; Twilio puts it flat in
    `Body` and `From`. Both are accepted because which one an installation uses
    is not this platform's decision.
    """
    message = _first_whatsapp_message(payload)
    if message is not None:
        sender = str(message.get("from") or "")
        text = str((message.get("text") or {}).get("body") or "")
    else:
        sender = str(payload.get("From") or "").replace("whatsapp:", "")
        text = str(payload.get("Body") or "")
    return CommandEnvelope(
        channel=Channel.WHATSAPP,
        text=text,
        external_user_id=sender,
        # A phone number is the conversation. There are no threads.
        conversation_id=sender,
        metadata={"provider": "cloud_api" if message is not None else "twilio"},
    )


def to_whatsapp(reply: Reply) -> dict[str, Any]:
    return {"messaging_product": "whatsapp", "type": "text",
            "text": {"body": reply.text[:_PHONE_LIMIT] or "(no answer)"}}


def from_voice(payload: dict[str, Any]) -> CommandEnvelope:
    """A transcribed utterance from a voice platform.

    Whatever did the transcription, what arrives here is text and a call id.
    Confidence travels in metadata so a low-confidence transcript can be handled
    as such rather than acted on.
    """
    return CommandEnvelope(
        channel=Channel.VOICE,
        text=str(payload.get("transcript") or payload.get("text") or ""),
        external_user_id=str(payload.get("caller") or payload.get("from") or ""),
        conversation_id=str(payload.get("call_id") or payload.get("session_id") or ""),
        metadata={"confidence": payload.get("confidence")},
    )


def to_voice(reply: Reply) -> dict[str, Any]:
    """One sentence, meant to be heard once.

    There is no scrollback in a phone call: anything not understood the first
    time is gone. The gateway has already stripped the parts that cannot be
    spoken.
    """
    return {"speech": reply.text[:400] or "I have nothing to report.", "end_call": False}


def from_ci(payload: dict[str, Any]) -> CommandEnvelope:
    """A pipeline asking for a run. No conversation, no ambiguity tolerated."""
    return CommandEnvelope(
        channel=Channel.CI,
        text=str(payload.get("instruction") or payload.get("text") or ""),
        external_user_id=str(payload.get("triggered_by") or "ci"),
        conversation_id=str(payload.get("pipeline_id") or payload.get("commit") or ""),
        project_id=str(payload.get("project_id") or ""),
        metadata={
            "commit": payload.get("commit", ""),
            "branch": payload.get("branch", ""),
            "pipeline_url": payload.get("pipeline_url", ""),
        },
    )


def to_ci(reply: Reply) -> dict[str, Any]:
    """Machine-shaped, because a pipeline branches on this rather than reading it."""
    return {
        "ok": reply.ok,
        "kind": reply.kind,
        "run_id": reply.run_id,
        "message": reply.text,
        "error": reply.error,
    }


# --------------------------------------------------------------------------- #
def _first_whatsapp_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Dig the message out of the Cloud API's nesting, or admit there is none.

    `entry[].changes[].value.messages[]`, every level of which is optional and
    any of which may be a status callback rather than a message.
    """
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            messages = ((change or {}).get("value") or {}).get("messages") or []
            if messages:
                return messages[0]
    return None


def _strip_mentions(text: str) -> str:
    """Remove the bot's own handle from the front of a message."""
    import re

    cleaned = re.sub(r"<@[A-Z0-9]+>", " ", text)
    cleaned = re.sub(r"<at>.*?</at>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", cleaned).strip()


#: Which parser and renderer belong to each channel, so the HTTP layer is a
#: lookup rather than a chain of `if`s that has to be edited per channel.
ADAPTERS = {
    Channel.SLACK: (from_slack, to_slack),
    Channel.TEAMS: (from_teams, to_teams),
    Channel.WHATSAPP: (from_whatsapp, to_whatsapp),
    Channel.VOICE: (from_voice, to_voice),
    Channel.CI: (from_ci, to_ci),
}
