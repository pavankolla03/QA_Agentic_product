"""Remembering which conversation belongs to which project.

A person in a Slack thread says "automate the checkout page". They have not said
which repository, because from where they are standing there is only one. The
binding between a conversation and a project has to live somewhere, and it
cannot live in the adapter: the same person moving from Slack to WhatsApp is the
same person, and a binding that evaporates when they switch surface is a binding
that gets re-asked for every day.

Sessions are per conversation rather than per user, on purpose. Two threads
about two services are two contexts, and carrying one project across both is how
a run gets started against the wrong repository — quietly, because nothing about
the message looked wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from packages.aiqa_types.models import new_id
from services.gateway.envelope import Channel, CommandEnvelope
from services.observability.db import session_scope
from services.observability.models import ChannelSessionRow, ProjectRow


@dataclass
class Session:
    """What this conversation has established so far."""

    id: str
    key: str
    channel: Channel
    external_user_id: str
    project_id: str = ""
    org_id: str = ""
    #: The run this conversation last started, so "how is it going?" and "stop"
    #: mean something without anybody quoting an id.
    last_run_id: str = ""
    metadata: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


class SessionStore:
    """Where conversations and their project bindings are kept."""

    def resolve(self, envelope: CommandEnvelope) -> Session:
        """The session for this envelope, creating it if this is the first message."""
        key = envelope.session_key
        with session_scope() as db:
            row = db.execute(
                select(ChannelSessionRow).where(ChannelSessionRow.key == key)
            ).scalars().first()

            if row is None:
                row = ChannelSessionRow(
                    id=new_id("chs"),
                    key=key,
                    channel=envelope.channel.value,
                    external_user_id=envelope.external_user_id,
                    conversation_id=envelope.conversation_id,
                    project_id=envelope.project_id,
                    org_id=envelope.org_id,
                )
                db.add(row)
            elif envelope.project_id and envelope.project_id != row.project_id:
                # The caller knows better than the memory does: a VS Code
                # workspace states its project on every message.
                row.project_id = envelope.project_id

            row.last_seen_at = datetime.now(timezone.utc)
            row.message_count = (row.message_count or 0) + 1
            return Session(
                id=row.id,
                key=row.key,
                channel=Channel(row.channel),
                external_user_id=row.external_user_id,
                project_id=row.project_id or "",
                org_id=row.org_id or envelope.org_id,
                last_run_id=row.last_run_id or "",
                metadata=dict(row.metadata_json or {}),
            )

    def bind_project(self, session_key: str, project_id: str) -> None:
        with session_scope() as db:
            row = db.execute(
                select(ChannelSessionRow).where(ChannelSessionRow.key == session_key)
            ).scalars().first()
            if row is not None:
                row.project_id = project_id

    def remember_run(self, session_key: str, run_id: str) -> None:
        with session_scope() as db:
            row = db.execute(
                select(ChannelSessionRow).where(ChannelSessionRow.key == session_key)
            ).scalars().first()
            if row is not None:
                row.last_run_id = run_id

    # ------------------------------------------------------------------ #
    @staticmethod
    def only_project(org_id: str = "") -> str:
        """The project to assume when a conversation has never named one.

        Only ever returns something when the answer is unambiguous. Picking the
        newest of several would start a run against whichever repository
        happened to be registered last, which is a coin toss dressed up as a
        default — and the person would not find out until they read the diff.
        """
        with session_scope() as db:
            stmt = select(ProjectRow.id)
            if org_id:
                stmt = stmt.where(ProjectRow.org_id == org_id)
            ids = list(db.execute(stmt.limit(2)).scalars())
        return ids[0] if len(ids) == 1 else ""
