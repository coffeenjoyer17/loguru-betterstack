"""Tests for BetterStackSink without hitting the network.

Strategy: stand up a tiny ``http.server`` on localhost, point the sink at it
via the ``host`` parameter, and inspect what arrives. This catches the actual
HTTP layer (auth header, content-type, JSON body shape) instead of mocking
``urllib`` at the boundary.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from loguru import logger

from loguru_betterstack import BetterStackSink


class _CapturingHandler(BaseHTTPRequestHandler):
    received: list[dict] = []
    headers_seen: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        self.__class__.received.append(json.loads(raw))
        self.__class__.headers_seen.append(dict(self.headers.items()))
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args, **kwargs) -> None:  # silence test noise
        return


@pytest.fixture
def http_server():
    _CapturingHandler.received = []
    _CapturingHandler.headers_seen = []
    server = HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_sink_ships_a_simple_record(http_server):
    sink = BetterStackSink(
        source_token="tok_test",
        host=http_server,
        batch_size=1,
        flush_interval=0.2,
    )
    # Reset Loguru handlers so we don't bleed into other tests.
    logger.remove()
    logger.add(sink, level="DEBUG")

    logger.info("hello world")

    assert _wait_until(lambda: len(_CapturingHandler.received) >= 1)
    sink.close(timeout=2.0)

    body = _CapturingHandler.received[0]
    assert isinstance(body, list)
    assert len(body) == 1
    record = body[0]
    assert record["level"] == "INFO"
    assert record["message"] == "hello world"
    assert "dt" in record


def test_sink_uses_bearer_auth(http_server):
    sink = BetterStackSink(
        source_token="tok_secret",
        host=http_server,
        batch_size=1,
        flush_interval=0.2,
    )
    logger.remove()
    logger.add(sink, level="INFO")

    logger.info("auth check")

    assert _wait_until(lambda: len(_CapturingHandler.headers_seen) >= 1)
    sink.close(timeout=2.0)

    headers = _CapturingHandler.headers_seen[0]
    assert headers.get("Authorization") == "Bearer tok_secret"
    assert headers.get("Content-Type") == "application/json"


def test_sink_batches_multiple_records(http_server):
    sink = BetterStackSink(
        source_token="tok",
        host=http_server,
        batch_size=5,
        flush_interval=10.0,  # large enough that count triggers the flush
    )
    logger.remove()
    logger.add(sink, level="DEBUG")

    for i in range(5):
        logger.info(f"msg {i}")

    assert _wait_until(lambda: len(_CapturingHandler.received) >= 1)
    sink.close(timeout=2.0)

    # All five records should arrive in a single request.
    assert sum(len(b) for b in _CapturingHandler.received) == 5


def test_sink_preserves_bound_context(http_server):
    sink = BetterStackSink(
        source_token="tok",
        host=http_server,
        batch_size=1,
        flush_interval=0.2,
    )
    logger.remove()
    logger.add(sink, level="DEBUG")

    logger.bind(user_id=42, route="/x").warning("contextual")

    assert _wait_until(lambda: len(_CapturingHandler.received) >= 1)
    sink.close(timeout=2.0)

    record = _CapturingHandler.received[0][0]
    assert record["context"] == {"user_id": 42, "route": "/x"}
    assert record["level"] == "WARNING"


def test_close_drains_pending_records(http_server):
    sink = BetterStackSink(
        source_token="tok",
        host=http_server,
        batch_size=100,        # so size never triggers a flush
        flush_interval=10.0,   # so time never triggers a flush
    )
    logger.remove()
    logger.add(sink, level="INFO")

    for i in range(3):
        logger.info(f"queued {i}")

    sink.close(timeout=3.0)

    total = sum(len(b) for b in _CapturingHandler.received)
    assert total == 3
