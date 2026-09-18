"""Credentials for the application under test.

Everything worth testing in a real application is behind a sign-in. A crawler
that cannot sign in sees the login page, follows every navigation link, gets
redirected back to the login page, and records five copies of it — which is
exactly what this platform did, and why a plan built from that crawl reads as
invented. It was invented: there was nothing else to build one from.

**Where these live, and why it is not the database.** A test account's password
is a secret belonging to the environment under test, and the control plane's
database is the wrong home for it: it is shared, it is backed up, it is read by
every dashboard query, and this platform has no encryption-at-rest to offer it.
Instead the credentials sit in the project's own `.aiqa/` directory, on the
machine that already holds the repository and the application's source. A worker
reads them from the same workspace it checks the code out of.

**They never reach a model.** No prompt is built from them, no run row records
them, no event carries them. The only thing that ever sees the password is the
browser typing it into the field it belongs in.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Where a project's credentials live, relative to its root.
CREDENTIALS_PATH = Path(".aiqa") / "credentials.json"

#: Environment fallback, for CI where there is no interactive session to type
#: them into and writing a secret to disk is the wrong move.
_ENV_USER = "AIQA_APP_USERNAME"
_ENV_PASSWORD = "AIQA_APP_PASSWORD"


@dataclass
class AppCredentials:
    """One account the platform may sign in as."""

    username: str = ""
    password: str = ""
    #: Where the sign-in form is, when it is not the page the crawl starts on.
    login_path: str = ""
    #: Anything else the form demands — a tenant, a domain, a one-off field.
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.username and self.password)

    def as_browser_payload(self) -> dict[str, Any]:
        """What the crawler needs. Deliberately not `asdict` — see `__repr__`."""
        return {
            "username": self.username,
            "password": self.password,
            "loginPath": self.login_path,
            "extra": dict(self.extra),
        }

    def __repr__(self) -> str:
        """Never print the password.

        A dataclass repr in a traceback, a log line or a debugger watch window
        is the most common way a secret escapes, and it happens at exactly the
        moment nobody is thinking about secrets.
        """
        return f"AppCredentials(username={self.username!r}, password=<redacted>)"


# --------------------------------------------------------------------------- #
# Reading them out of a sentence
# --------------------------------------------------------------------------- #
#: Labelled forms only. Two bare tokens after a URL are far more often a stray
#: word than a password, and guessing wrong means typing somebody's message into
#: a login form.
_USER_RE = re.compile(
    r"\b(?:user(?:name)?|login|account|email|id)\s*(?:[:=]|is)\s*[\"']?([^\s\"',;]+)",
    re.IGNORECASE,
)
_PASSWORD_RE = re.compile(
    r"\b(?:pass(?:word|wd)?|pwd|secret)\s*(?:[:=]|is)\s*[\"']?([^\s\"',;]+)",
    re.IGNORECASE,
)
#: "sign in with alice / hunter2" — a slash pair, but only right after an
#: explicit credential word, so an ordinary "and/or" cannot match.
_PAIR_RE = re.compile(
    r"\b(?:credentials?|creds|sign\s*in\s*(?:with|as)|log\s*in\s*(?:with|as))\s*[:=]?\s*"
    r"[\"']?([^\s\"'/,;]+)[\"']?\s*[/|]\s*[\"']?([^\s\"'/,;]+)",
    re.IGNORECASE,
)


def parse(message: str) -> tuple[str, AppCredentials | None]:
    """Pull credentials out of a message, returning the message without them.

    The cleaned message is what gets stored as the run's instruction and shown
    in every UI afterwards, so this is the only thing standing between a typed
    password and a permanent record of it.
    """
    text = message or ""
    pair = _PAIR_RE.search(text)
    if pair:
        credentials = AppCredentials(username=pair.group(1), password=pair.group(2))
        return _tidy(text.replace(pair.group(0), " ")), credentials

    user = _USER_RE.search(text)
    password = _PASSWORD_RE.search(text)
    if not (user and password):
        return _tidy(text), None

    credentials = AppCredentials(username=user.group(1), password=password.group(1))
    cleaned = text.replace(user.group(0), " ").replace(password.group(0), " ")
    return _tidy(cleaned), credentials


def _tidy(text: str) -> str:
    return re.sub(r"\s{2,}", " ", (text or "").replace(" ,", ",")).strip(" ,;:")


# --------------------------------------------------------------------------- #
# Storing them next to the project
# --------------------------------------------------------------------------- #
def save(project_root: str | Path, credentials: AppCredentials) -> Path:
    """Write credentials into the project, readable only by this user."""
    path = Path(project_root) / CREDENTIALS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "username": credentials.username,
                "password": credentials.password,
                "login_path": credentials.login_path,
                "extra": credentials.extra,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    # Best effort: POSIX honours this, Windows ignores the group/other bits.
    # Worth doing anyway, because the platform is run on both.
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    _ignore(path.parent)
    return path


def load(project_root: str | Path) -> AppCredentials | None:
    """The project's credentials, from disk or the environment.

    The environment wins. A CI job sets variables rather than writing secrets
    into a checkout, and if it has bothered to set them it means them.
    """
    env_user, env_password = os.getenv(_ENV_USER, ""), os.getenv(_ENV_PASSWORD, "")
    if env_user and env_password:
        return AppCredentials(username=env_user, password=env_password)

    path = Path(project_root) / CREDENTIALS_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return AppCredentials(
        username=str(data.get("username") or ""),
        password=str(data.get("password") or ""),
        login_path=str(data.get("login_path") or ""),
        extra={str(k): str(v) for k, v in (data.get("extra") or {}).items()},
    )


def _ignore(aiqa_dir: Path) -> None:
    """Make sure the file cannot be committed.

    A password written into a checkout is one `git add -A` away from a public
    repository, and the person who typed it into a chat panel has no reason to
    expect a file was created at all.
    """
    marker = aiqa_dir / ".gitignore"
    line = "credentials.json"
    try:
        existing = marker.read_text(encoding="utf-8") if marker.exists() else ""
        if line not in existing.split():
            prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
            marker.write_text(
                f"{prefix}# never commit the application's test credentials\n{line}\n",
                encoding="utf-8",
            )
    except OSError:
        pass
