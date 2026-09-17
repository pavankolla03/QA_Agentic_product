"""One shape for "somebody asked for something", whatever they asked it through.

The platform's understanding of a message — is this a greeting, a question it
can answer from its own records, or a request to automate an application? — is
worth exactly one implementation. Until now it lived inside the VS Code chat
endpoint, which meant Slack, WhatsApp, a voice assistant and a CI job could each
have their own approximation of it, and they would drift apart within a month.

So a channel adapter does two things and no more: turn whatever arrived into a
:class:`CommandEnvelope`, and turn the :class:`Reply` it gets back into whatever
that channel renders. Everything between those two points is shared.

The envelope deliberately carries the *channel's own* identifiers rather than
this platform's. A Slack user is a Slack user id; resolving that to a project
and a permission set is the gateway's job, not the adapter's, because that
resolution is a security decision and security decisions should not be
reimplemented per channel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class Channel(StrEnum):
    """Where a command came from.

    Worth recording for its own sake: "who can approve a diff" and "how much
    detail is worth rendering" both differ by channel, and an audit trail that
    cannot say whether a run was started from an IDE or from a phone is missing
    the thing an auditor asks first.
    """

    VSCODE = "vscode"
    API = "api"
    CLI = "cli"
    SLACK = "slack"
    TEAMS = "teams"
    WHATSAPP = "whatsapp"
    VOICE = "voice"
    CI = "ci"
    JIRA = "jira"

    @property
    def is_conversational(self) -> bool:
        """Does a person read the reply as it arrives?

        Governs verbosity, not permissions. A CI job wants the whole report; a
        voice call wants one sentence it can say out loud.
        """
        return self in {
            Channel.VSCODE,
            Channel.SLACK,
            Channel.TEAMS,
            Channel.WHATSAPP,
            Channel.VOICE,
        }

    @property
    def is_voice(self) -> bool:
        """Is this spoken aloud?

        Spoken replies cannot contain a file path, a diff or a run id and remain
        listenable, so they are written differently rather than truncated.
        """
        return self is Channel.VOICE


@dataclass
class CommandEnvelope:
    """One inbound command, in the only form the gateway accepts."""

    channel: Channel
    text: str
    #: Who, in the channel's own namespace: a Slack member id, a phone number,
    #: a VS Code installation. Never assumed to mean anything here.
    external_user_id: str = ""
    #: The thread, room, chat or call this belongs to. Two messages sharing one
    #: of these are the same conversation, and that is what makes "run it again"
    #: resolvable without the sender repeating themselves.
    conversation_id: str = ""
    #: Set when the channel already knows; resolved from the session otherwise.
    project_id: str = ""
    org_id: str = ""
    #: Anything channel-specific worth keeping for the audit trail.
    metadata: dict[str, Any] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def session_key(self) -> str:
        """What makes two messages part of one conversation.

        Channel-scoped on purpose: a Slack thread id and a WhatsApp chat id can
        collide, and a collision here would hand one person another person's
        project binding.
        """
        return f"{self.channel.value}:{self.conversation_id or self.external_user_id}"


@dataclass
class Reply:
    """What to say back, before any channel has formatted it.

    `text` is always present and always makes sense on its own — a channel that
    can render nothing else can render this. The rest is detail a richer surface
    may use and a poorer one may drop.
    """

    text: str = ""
    #: "reply" when the answer is the text; "run" when work was started.
    kind: str = "reply"
    run_id: str = ""
    suggestions: list[str] = field(default_factory=list)
    #: The application a run was pointed at, when one was named.
    target_url: str = ""
    #: True when the run's scope is whatever the crawl finds rather than what
    #: the message asked for. Worth saying out loud before it begins.
    autopilot: bool = False
    #: Set when the gateway refused. Separate from `text` so a channel can
    #: style it differently, and so nothing has to parse prose to notice.
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error
