"""Recognising an application in a sentence.

Small and dependency-free on purpose. Both the intent layer and the requirement
agent need to answer "is this message just a URL?", and they sit on opposite
sides of the engine, so anything they share has to be importable from either
without dragging the orchestrator in behind it.
"""

from __future__ import annotations

import re

#: A URL, with or without a scheme. Bare hosts are accepted because people type
#: "localhost:3000" and "shop.example.com" far more often than they type the
#: scheme, and refusing those would make the headline feature feel broken.
URL_RE = re.compile(
    r"""(?xi)
    \b(
        https?://[^\s<>"']+
      | localhost(?::\d+)?(?:/[^\s<>"']*)?
      | (?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:/[^\s<>"']*)?
      | (?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}(?::\d+)?(?:/[^\s<>"']*)?
    )
    """
)

#: Hosts on somebody's own machine or private network rather than the public
#: internet: loopback and the three RFC 1918 ranges. None of these has a
#: certificate, so assuming HTTPS for them guarantees a failed connection.
_LOCAL_RE = re.compile(
    r"""(?xi)^(
        localhost
      | 127\.\d{1,3}\.\d{1,3}\.\d{1,3}
      | 0\.0\.0\.0
      | \[::1\]
      | 10\.\d{1,3}\.\d{1,3}\.\d{1,3}
      | 192\.168\.\d{1,3}\.\d{1,3}
      | 172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}
    )(?::|/|$)"""
)

#: Words that add nothing to a URL. If everything around the URL is one of
#: these, the message means "this application" and nothing narrower.
_GENERIC_AROUND_URL = frozenset(
    {
        "automate", "test", "tests", "testing", "qa", "check", "cover", "coverage",
        "explore", "crawl", "scan", "do", "run", "please", "can", "you", "could",
        "this", "that", "it", "the", "a", "an", "my", "our", "for", "me", "us",
        "at", "on", "in", "of", "from", "with", "and", "here", "s", "url", "link",
        "site", "website", "web", "app", "application", "whole", "entire", "full",
        "all", "everything", "complete", "completely", "thoroughly", "end", "to",
        "figure", "out", "start", "go", "ahead", "now", "http", "https", "www",
    }
)


def extract_url(message: str) -> str:
    """The application this message points at, if it points at one."""
    match = URL_RE.search(message or "")
    if not match:
        return ""
    url = match.group(0).rstrip(".,;:!?)]}'\"")
    if "://" in url:
        return url
    # A dev server is not on HTTPS. Defaulting "localhost:3000" to https means
    # the very first thing autopilot does against a local app is fail to connect.
    scheme = "http" if _LOCAL_RE.match(url) else "https"
    return f"{scheme}://{url}"


def is_bare_url_request(text: str) -> bool:
    """Is what is left around the URL just a way of saying "this one"?

    "https://shop.example.com" and "automate the whole application at
    https://shop.example.com" mean the same thing. "automate the login page at
    https://shop.example.com" does not - it names a target, and naming a target
    is the difference between autopilot and an ordinary run.

    `text` must already have the URL removed.
    """
    words = [word for word in re.split(r"[^a-z0-9]+", (text or "").lower()) if word]
    return all(word in _GENERIC_AROUND_URL for word in words)


def autopilot_target(message: str) -> str:
    """The URL to crawl when a message is nothing but a URL, else "".

    One function so that every channel - chat, CLI, Slack, an API caller - gets
    the same answer to "did the user just hand me an application?".
    """
    url = extract_url(message)
    if not url:
        return ""
    residue = URL_RE.sub(" ", message or "")
    return url if is_bare_url_request(residue) else ""
