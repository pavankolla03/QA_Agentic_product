"""`aiqa serve` must not double-bind a live port.

Windows lets a second process bind a port that is already serving, and then
splits requests between them. Nothing errors: the control plane simply answers
roughly half the time and hangs for the rest, which from the extension is
indistinguishable from a chat that does not work. Two accumulated during one
debugging session before anyone noticed the process list.
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from services.api_gateway.cli import _already_serving


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        ok = self.path == "/api/health"
        self.send_response(200 if ok else 404)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}' if ok else b"")

    def log_message(self, *_: object) -> None:
        pass


@pytest.fixture
def serving_port() -> int:
    server = HTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_a_live_control_plane_is_detected(serving_port: int) -> None:
    assert _already_serving("127.0.0.1", serving_port) is True


def test_a_free_port_is_not(serving_port: int) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert _already_serving("127.0.0.1", free) is False


def test_a_wildcard_bind_probes_loopback(serving_port: int) -> None:
    """`--host 0.0.0.0` cannot be connected to by that name on every platform."""
    assert _already_serving("0.0.0.0", serving_port) is True
    assert _already_serving("", serving_port) is True


def test_something_that_is_not_us_does_not_count(serving_port: int) -> None:
    """A port held by another program answers, but not with our health check."""

    class _Other(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Other)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert _already_serving("127.0.0.1", server.server_address[1]) is False
    finally:
        server.shutdown()
        server.server_close()
