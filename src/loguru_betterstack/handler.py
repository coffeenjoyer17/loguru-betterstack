"""
BetterStackSink — a Loguru sink that batches log records and ships them to
Better Stack's HTTP ingestion endpoint.

Better Stack accepts a JSON array per request, with each entry as a free-form
object. The standard fields are:

- ``dt`` — RFC 3339 timestamp or epoch seconds/ms/ns
- ``level`` — log level string (Better Stack groups by this)
- ``message`` — human-readable line

Anything else is preserved as structured metadata. Loguru's ``record`` dict is
flattened into that shape: extra fields go under ``context``, exception info
goes into ``exception``, and the source location is preserved under ``source``.

Design notes
------------

- The sink is synchronous from the caller's perspective: ``sink(message)`` is
  fast (queue append + maybe trigger flush). Network I/O happens on a single
  background daemon thread.
- Records are flushed when the in-memory queue reaches ``batch_size`` *or* on
  a fixed ``flush_interval``. Whichever comes first.
- ``close()`` blocks until the queue is drained. ``__del__`` calls ``close``
  on a best-effort basis. For programs that use ``sys.exit`` cleanly, register
  it with ``atexit`` (the public ``register_atexit`` helper does this).
- Network failures are logged to stderr but do not raise — the design goal is
  to never let observability infrastructure crash the host application.
"""

from __future__ import annotations

import atexit
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

DEFAULT_HOST = "in.logs.betterstack.com"
DEFAULT_BATCH_SIZE = 100
DEFAULT_FLUSH_INTERVAL = 5.0  # seconds
DEFAULT_MAX_QUEUE = 10_000
DEFAULT_TIMEOUT = 10.0  # seconds


class BetterStackSink:
    """A callable Loguru sink that ships logs to Better Stack.

    Parameters
    ----------
    source_token:
        The "Source Token" from your Better Stack source. Sent as a Bearer
        token in the ``Authorization`` header.
    host:
        The ingesting host for your source (defaults to ``in.logs.betterstack.com``).
        Use the host shown in your source settings; some accounts use
        regional hosts like ``s1234567.eu-nbg-2.betterstackdata.com``.
    batch_size:
        Maximum number of records buffered before forcing a flush.
    flush_interval:
        Maximum seconds a record may sit in the buffer before being flushed.
    max_queue:
        Hard cap on in-memory queue size. New records are dropped (with a
        warning to stderr) once this is exceeded; this protects against
        unbounded memory growth if the upstream is unreachable.
    timeout:
        Per-request HTTP timeout in seconds.
    """

    def __init__(
        self,
        source_token: str,
        *,
        host: str = DEFAULT_HOST,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        max_queue: int = DEFAULT_MAX_QUEUE,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not source_token:
            raise ValueError("source_token is required")

        self._token = source_token
        if host.startswith(("http://", "https://")):
            self._url = host.rstrip("/")
        else:
            self._url = f"https://{host.strip('/')}"
        self._batch_size = max(1, int(batch_size))
        self._flush_interval = max(0.1, float(flush_interval))
        self._max_queue = max(self._batch_size, int(max_queue))
        self._timeout = float(timeout)

        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self._max_queue)
        self._stop = threading.Event()
        self._dropped = 0
        self._worker = threading.Thread(target=self._run, name="loguru-betterstack", daemon=True)
        self._worker.start()

    # -- Loguru sink interface ------------------------------------------------

    def __call__(self, message: Any) -> None:
        """Loguru calls this for each log record.

        ``message`` is a Loguru ``Message`` object — string-like, with a
        ``.record`` attribute holding the full record dict.
        """
        record = getattr(message, "record", None)
        if record is None:
            # Fall back to a plain text record if Loguru gave us a raw string.
            payload: dict[str, Any] = {
                "dt": _now_iso(),
                "message": str(message),
            }
        else:
            payload = _flatten_record(record)

        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                print(
                    f"[loguru-betterstack] queue full; dropped {self._dropped} record(s)",
                    file=sys.stderr,
                )

    # -- Lifecycle ------------------------------------------------------------

    def close(self, timeout: float | None = None) -> None:
        """Flush pending records and stop the worker.

        Safe to call multiple times. Pass ``timeout=None`` to wait indefinitely.
        """
        if self._stop.is_set():
            return
        self._stop.set()
        self._worker.join(timeout=timeout)

    def register_atexit(self) -> None:
        """Register :meth:`close` to run at interpreter shutdown."""
        atexit.register(self.close)

    def __del__(self) -> None:
        try:
            self.close(timeout=2.0)
        except Exception:
            pass

    # -- Internals ------------------------------------------------------------

    def _run(self) -> None:
        batch: list[dict[str, Any]] = []
        next_flush = time.monotonic() + self._flush_interval

        while not self._stop.is_set() or not self._queue.empty():
            timeout = max(0.0, next_flush - time.monotonic())
            try:
                item = self._queue.get(timeout=timeout)
                batch.append(item)
            except queue.Empty:
                pass

            now = time.monotonic()
            should_flush = batch and (len(batch) >= self._batch_size or now >= next_flush)
            if should_flush:
                self._send(batch)
                batch = []
                next_flush = time.monotonic() + self._flush_interval

        if batch:
            self._send(batch)

    def _send(self, batch: list[dict[str, Any]]) -> None:
        body = json.dumps(batch).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                # Drain the body so the connection can be reused.
                resp.read()
        except urllib.error.HTTPError as exc:
            print(
                f"[loguru-betterstack] HTTP {exc.code}: {exc.reason} for {len(batch)} record(s)",
                file=sys.stderr,
            )
        except urllib.error.URLError as exc:
            print(
                f"[loguru-betterstack] network error: {exc.reason} for {len(batch)} record(s)",
                file=sys.stderr,
            )
        except Exception as exc:  # pragma: no cover - last-resort guard
            print(
                f"[loguru-betterstack] unexpected error: {exc!r} for {len(batch)} record(s)",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flatten_record(record: dict[str, Any]) -> dict[str, Any]:
    """Project a Loguru record dict into Better Stack's preferred shape."""
    payload: dict[str, Any] = {
        "dt": _format_dt(record.get("time")),
        "level": str(record.get("level", "INFO")).strip()
        if not _is_loguru_level(record.get("level"))
        else record["level"].name,
        "message": str(record.get("message", "")),
    }

    source: dict[str, Any] = {}
    for key in ("name", "function", "module", "line"):
        value = record.get(key)
        if value is not None:
            source[key] = _stringify(value)
    proc = record.get("process")
    if proc is not None:
        source["process"] = _id_name(proc)
    thread = record.get("thread")
    if thread is not None:
        source["thread"] = _id_name(thread)
    file_obj = record.get("file")
    if file_obj is not None:
        source["file"] = getattr(file_obj, "name", _stringify(file_obj))
    if source:
        payload["source"] = source

    extra = record.get("extra")
    if isinstance(extra, dict) and extra:
        payload["context"] = {k: _safe_value(v) for k, v in extra.items()}

    exception = record.get("exception")
    if exception is not None:
        payload["exception"] = _stringify(exception)

    return payload


def _format_dt(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return _now_iso()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_loguru_level(level: Any) -> bool:
    return level is not None and hasattr(level, "name")


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return str(value)


def _id_name(value: Any) -> dict[str, Any]:
    """Project a Loguru ``RecordThread`` / ``RecordProcess`` into ``{id, name}``."""
    out: dict[str, Any] = {}
    pid = getattr(value, "id", None)
    if pid is not None:
        out["id"] = pid
    pname = getattr(value, "name", None)
    if pname is not None:
        out["name"] = pname
    if not out:
        return {"value": _stringify(value)}
    return out


def _safe_value(value: Any) -> Any:
    """Best-effort coerce arbitrary values to JSON-friendly types."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe_value(v) for k, v in value.items()}
    return repr(value)
