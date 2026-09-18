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


def test_a_port_held_by_something_else_still_stops_us(serving_port: int) -> None:
    """Held is held, whoever is holding it.

    This used to return False so that startup proceeded and the bind failed with
    the operating system's message. That is a worse outcome than it sounds:
    startup bookkeeping runs before the bind, and the reconciler's premise —
    "at startup nothing is in flight" — is false for a second process. One such
    failed start marked a healthy run on the first instance as `failed`.

    We cannot have the port either way. Refusing early costs a clearer message
    and nothing else.
    """

    class _Other(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Other)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert _already_serving("127.0.0.1", server.server_address[1]) is True
    finally:
        server.shutdown()
        server.server_close()


def test_a_free_port_is_free() -> None:
    """The guard must not refuse to start for no reason."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert _already_serving("127.0.0.1", free) is False


def test_a_busy_server_is_not_mistaken_for_a_free_port() -> None:
    """The socket decides, not the health endpoint.

    The guard asked `/api/health` — the one endpoint that contacts every model
    provider — with a two-second timeout. A control plane busy with a run
    answered late, the probe gave up, and the guard reported the port free.
    A listener that never answers at all stands in for that here.
    """
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        assert _already_serving("127.0.0.1", listener.getsockname()[1]) is True
    finally:
        listener.close()
