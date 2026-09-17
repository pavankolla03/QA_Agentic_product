"""The one place that decides what a message means.

Every channel funnels through `handle`. What arrives is a
:class:`CommandEnvelope`; what comes back is a :class:`Reply` that no channel
has formatted yet. In between sits exactly the logic the VS Code chat endpoint
already had — resolve the intent, answer it from the database if the answer is a
fact, otherwise start a run — and nothing else.

Keeping it to one implementation is the entire point. Six channels with six
approximations of "is this a greeting or a request to automate an application?"
would agree in week one and disagree by week four, and the disagreements would
be discovered by someone whose run did not start.

What the gateway will not do is guess a project. A message that never named one,
in an installation with several, is refused with a question rather than pointed
at whichever repository was registered most recently. Starting the wrong run is
not a smaller error than starting none: it writes files.
"""

from __future__ import annotations

import logging

from packages.aiqa_types.models import RunRequest
from services.agent_engine.conversation import ConversationService
from services.agent_engine.intents import Intent, resolve
from services.gateway.envelope import Channel, CommandEnvelope, Reply
from services.gateway.sessions import Session, SessionStore

log = logging.getLogger("aiqa.gateway")

#: What a spoken channel is told instead of a run id. A phone call cannot hear
#: "run_8f0dc7de655a4dc2" and do anything useful with it.
_VOICE_STARTED = (
    "Starting now. It takes a few minutes on free models. Ask me how it is going "
    "whenever you like."
)


class Gateway:
    """Turns an envelope from any channel into a reply and, sometimes, a run."""

    def __init__(self, engine, sessions: SessionStore | None = None) -> None:
        self.engine = engine
        self.sessions = sessions or SessionStore()

    # ------------------------------------------------------------------ #
    async def handle(self, envelope: CommandEnvelope) -> Reply:
        """One command in, one reply out."""
        text = (envelope.text or "").strip()
        if not text:
            return Reply(text="I did not catch that. Say what you would like automated.")

        session = self.sessions.resolve(envelope)
        resolution = resolve(text)

        if resolution.intent.starts_a_run:
            return await self._start_run(envelope, session, resolution, text)

        answer = ConversationService(
            project_id=session.project_id, org_id=session.org_id
        ).answer(resolution)
        if answer is not None:
            return Reply(
                text=self._for_channel(envelope.channel, answer.text),
                suggestions=[] if envelope.channel.is_voice else (answer.suggestions or []),
            )

        # Nothing deterministic fits. Only a conversational channel is worth
        # spending a model call on: a CI job asking something unparseable wants
        # an error it can act on, not a paragraph.
        if not envelope.channel.is_conversational:
            return Reply(
                error="not understood",
                text=(
                    f'"{text[:80]}" is not a command I recognise. Send a URL to automate an '
                    "application, or a sentence describing the feature to cover."
                ),
            )
        return await self._ask_a_model(envelope, session, text)

    # ------------------------------------------------------------------ #
    async def _start_run(
        self, envelope: CommandEnvelope, session: Session, resolution, text: str
    ) -> Reply:
        project_id = session.project_id or self.sessions.only_project(session.org_id)
        if not project_id:
            # Deliberately a question rather than a default. Picking the most
            # recently registered project would start a run against a repository
            # nobody named, and the person would find out from the diff.
            return Reply(
                error="no project",
                text=(
                    "I do not know which project this conversation is about. Tell me the "
                    "project name, or bind this conversation to one, and I will start."
                ),
            )
        if project_id != session.project_id:
            self.sessions.bind_project(session.key, project_id)

        request = RunRequest(
            project_id=project_id,
            instruction=text,
            mode=resolution.mode,
            target_url=resolution.target_url or None,
            metadata={
                "channel": envelope.channel.value,
                "session_key": session.key,
                "external_user_id": envelope.external_user_id,
            },
        )
        try:
            run_id = self.engine.create_run(
                request, user_id=envelope.external_user_id, org_id=session.org_id
            )
        except Exception as exc:  # noqa: BLE001 - every channel gets a sentence, never a traceback
            log.warning("gateway could not create a run", exc_info=True)
            return Reply(error="run rejected", text=f"I could not start that: {exc}")

        self.sessions.remember_run(session.key, run_id)
        return Reply(
            kind="run",
            run_id=run_id,
            target_url=resolution.target_url,
            autopilot=resolution.intent is Intent.RUN_AUTOPILOT,
            text=self._starting(envelope.channel, resolution, run_id),
        )

    async def _ask_a_model(self, envelope: CommandEnvelope, session: Session, text: str) -> Reply:
        try:
            reply = await self.engine.answer(text, context=self._context(session))
        except Exception:  # noqa: BLE001 - a chat reply must never fail the channel
            log.debug("gateway model answer failed", exc_info=True)
            reply = ""
        if not reply:
            return Reply(
                text=(
                    "I could not reach a model in time. Ask me about a run, a failure or "
                    "today's cost and I will answer from my own records, which needs no "
                    "model at all."
                )
            )
        return Reply(text=self._for_channel(envelope.channel, reply))

    # ------------------------------------------------------------------ #
    @staticmethod
    def _starting(channel: Channel, resolution, run_id: str) -> str:
        if channel.is_voice:
            return _VOICE_STARTED
        if resolution.intent is Intent.RUN_AUTOPILOT:
            return (
                f"Nothing to go on but the URL, so I will crawl {resolution.target_url}, work "
                "out what it has, and automate that. Whatever I cannot see, I will not write a "
                f"test for.\n\nRun {run_id} is starting."
            )
        return f"Run {run_id} is starting."

    @staticmethod
    def _for_channel(channel: Channel, text: str) -> str:
        """Fit an answer to what the channel can actually carry.

        Only voice is rewritten, and only structurally: a spoken reply cannot
        contain a table, a file path or a run id and stay listenable. Everything
        else gets the text as written, because truncating prose to fit a
        notification is how the important half gets cut off.
        """
        if not channel.is_voice:
            return text
        spoken = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith(("-", "*", "#", "|"))
        ]
        return " ".join(spoken)[:400] if spoken else text[:400]

    @staticmethod
    def _context(session: Session) -> str:
        if not session.project_id:
            return "No project is bound to this conversation yet."
        return f"Project {session.project_id}, reached over {session.channel.value}."
