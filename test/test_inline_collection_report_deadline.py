"""A commit or drop report fits inside SIGTERM's join, in wall time.

``_close_collection`` bounds a report by ``COLLECTION_REPORT_DEADLINE_SECS``,
under ``OUTCOME_HOOK_SIGNAL_WAIT_SECS``. That only holds if the bound reaches
every place the report can spend time: ``mcp_core._send``'s refused-connection
re-dials and their pauses, and a reply that drips in one ``recv`` at a time.
The deadline is therefore passed to ``_send`` as an absolute monotonic reading.

The clock is a delegating stand-in for ``mcp_core.time`` (the consumer's own
alias), so no stdlib attribute is patched. This module is listed in the repo
conftest's ``_REAL_MCP_POST_MODULES``, so ``_post`` is real here and the
transport below it is scripted.
"""

from __future__ import annotations

import socket
import socketserver
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from kiro_crew import loopback_http, mcp_core, mcp_shared
from kiro_crew.mcp_tools import spawn as spawn_tools


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += secs


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clk = _Clock()
    monkeypatch.setattr(mcp_core, "time", clk)
    monkeypatch.setattr(spawn_tools, "_report_clock", clk.monotonic)
    target = ("http://127.0.0.1:9", "")
    monkeypatch.setattr(mcp_core, "_resolve_api_target", lambda: target)
    monkeypatch.setattr(mcp_core, "_replay_target", lambda _b: None)
    monkeypatch.setattr(mcp_core, "_reverify_refused_target", lambda _b: target)
    monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "s")
    monkeypatch.setattr(mcp_core, "_secret_for_base", lambda _b: "s")
    monkeypatch.setattr(mcp_core, "_caller_header", lambda: {})
    monkeypatch.setattr(mcp_core, "_session_token_header", lambda: {})
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "")
    return clk


def test_a_report_into_a_restarting_gateway_fits_the_sigterm_wait(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two refusals, then the rebound listener accepts and stalls. Re-dialled at
    the full timeout after 0.75 s of pauses, the report would run 4.25 s."""
    dials: list[float] = []

    def _urlopen(_req: Any, timeout: float, **_kw: Any):
        dials.append(timeout)
        if len(dials) <= 2:
            raise urllib.error.URLError(ConnectionRefusedError())
        clock.now += timeout  # accepted, then stalls for its whole timeout
        raise socket.timeout("timed out")

    monkeypatch.setattr(mcp_core, "_api_urlopen", _urlopen)
    spawn_tools._close_collection("dashboard:p", ["a1"], ["a1"], "commit")
    assert clock.now <= spawn_tools.COLLECTION_REPORT_DEADLINE_SECS
    assert (
        clock.now <= mcp_shared.OUTCOME_HOOK_SIGNAL_WAIT_SECS
    ), f"report ran {clock.now:.2f}s, past the {mcp_shared.OUTCOME_HOOK_SIGNAL_WAIT_SECS}s SIGTERM join"
    assert len(dials) == 3  # the healthy side: the retry still re-dials


def test_no_redial_starts_once_the_deadline_has_passed(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    dials: list[float] = []

    def _urlopen(_req: Any, timeout: float, **_kw: Any):
        dials.append(timeout)
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(mcp_core, "_api_urlopen", _urlopen)
    resp = mcp_core._post("/api/x", {}, timeout=5.0, deadline=0.2)
    # The 0.25 s pause is clipped to the 0.2 s left, and nothing is re-dialled after it.
    assert dials == [0.2]
    assert clock.now == pytest.approx(0.2)
    assert resp.get("refused") is True


def test_without_a_deadline_the_refused_retry_is_unchanged(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    dials: list[float] = []

    def _urlopen(_req: Any, timeout: float, **_kw: Any):
        dials.append(timeout)
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(mcp_core, "_api_urlopen", _urlopen)
    resp = mcp_core._post("/api/x", {}, timeout=5.0)
    assert dials == [5.0, 5.0, 5.0]
    assert clock.now == pytest.approx(sum(mcp_core._REFUSED_RETRY_BACKOFFS))
    assert resp.get("refused") is True


class _Sock:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)


class _DripReply:
    """A reply whose every chunk is one byte and costs 1 s of the clock."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self.sock = _Sock()
        self.fp = type("_Fp", (), {"raw": type("_Raw", (), {"_sock": self.sock})()})()

    def __enter__(self) -> "_DripReply":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def read(self, _amt: int = -1) -> bytes:
        self._clock.now += 1.0
        return b" "


def test_a_slow_drip_reply_is_bounded_in_wall_time(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A socket timeout bounds each recv, so a reply dripping one byte per recv
    would run for as many timeouts as it has bytes. The body read re-arms the
    socket to the time left before each chunk, and stops at the deadline."""
    reply = _DripReply(clock)
    monkeypatch.setattr(mcp_core, "_api_urlopen", lambda _req, timeout, **_kw: reply)
    resp = mcp_core._post("/api/x", {}, timeout=5.0, deadline=3.5)
    assert resp.get("error")
    assert resp.get("transport_error") is True  # it reached a gateway: acceptance unknown
    assert reply.sock.timeouts == [3.5, 2.5, 1.5, 0.5]
    assert clock.now == pytest.approx(4.0)  # the fake read ignores its socket timeout


def test_a_reply_inside_the_deadline_is_read_whole(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Reply(_DripReply):
        def __init__(self, clock: _Clock) -> None:
            super().__init__(clock)
            self._parts = [b'{"ok": ', b"true}", b""]

        def read(self, _amt: int = -1) -> bytes:
            return self._parts.pop(0)

    reply = _Reply(clock)
    monkeypatch.setattr(mcp_core, "_api_urlopen", lambda _req, timeout, **_kw: reply)
    assert mcp_core._post("/api/x", {}, timeout=5.0, deadline=3.5) == {"ok": True}


class _LoopbackServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # Skip http.server's socket.getfqdn lookup, which can stall on macOS.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class _OkHandler(BaseHTTPRequestHandler):
    """Answers every POST with a JSON body large enough to need several chunks."""

    body = b'{"ok": true, "pad": "' + b"x" * (3 * 64 * 1024) + b'"}'

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args: Any) -> None:
        return None


def test_a_real_reply_read_to_its_end_is_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over a real loopback socket. ``http.client`` closes the socket once the
    body is exhausted, so re-arming its timeout after the last chunk would raise
    EBADF and turn a request the gateway applied into a transport failure, which
    ``_close_collection`` then retries."""
    server = _LoopbackServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        target = (f"http://127.0.0.1:{server.server_port}", "")
        monkeypatch.setattr(mcp_core, "_resolve_api_target", lambda: target)
        monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "s")
        monkeypatch.setattr(mcp_core, "_caller_header", lambda: {})
        monkeypatch.setattr(mcp_core, "_session_token_header", lambda: {})
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "")
        monkeypatch.setattr(
            mcp_core,
            "_api_urlopen",
            lambda req, timeout, **_kw: loopback_http.loopback_urlopen(req, timeout=timeout),
        )
        resp = mcp_core._post("/api/x", {}, timeout=30.0, deadline=time.monotonic() + 30.0)
        assert resp.get("ok") is True and "error" not in resp
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
