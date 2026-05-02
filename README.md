# loguru-betterstack

Ship [Loguru](https://github.com/Delgan/loguru) logs to [Better Stack](https://betterstack.com/logs) over HTTP. Batched, non-blocking, zero external dependencies beyond Loguru.

```python
from loguru import logger
from loguru_betterstack import BetterStackSink

sink = BetterStackSink(
    source_token="YOUR_SOURCE_TOKEN",
    host="in.logs.betterstack.com",  # use the host shown in your source settings
)
sink.register_atexit()

logger.add(sink, level="INFO")
logger.info("Hello from loguru-betterstack")
logger.bind(user_id=42, route="/checkout").warning("Slow checkout")
```

That's it. Records are buffered in memory and flushed on a background thread:

- by **count** — every `batch_size` records (default 100), or
- by **time** — every `flush_interval` seconds (default 5).

The host application never blocks on the network, and a transient outage at Better Stack will never crash your service: failed sends are reported to stderr and the queue keeps draining.

## Why not the official Logtail SDK?

The official Better Stack Python SDK is great if you're using the standard `logging` module. If you've already standardized on Loguru — its bind/contextualize features, exceptions-by-default, and structured records — you don't want to bridge through `logging.Handler`. This package speaks Loguru natively.

## Install

```bash
pip install loguru-betterstack
```

(Or, if you want to track main: `pip install git+https://github.com/coffeenjoyer17/loguru-betterstack`)

## What you get on each record

Loguru's record dict is projected into Better Stack's preferred shape:

| Field        | Source                                       |
| ------------ | -------------------------------------------- |
| `dt`         | `record.time` as RFC 3339 UTC                |
| `level`      | `record.level.name`                          |
| `message`    | `record.message`                             |
| `source.*`   | `name`, `function`, `module`, `line`, `file`, `process`, `thread` |
| `context.*`  | everything from `logger.bind(...)` / `logger.contextualize(...)` |
| `exception`  | the formatted traceback when present         |

So `logger.bind(user_id=42).info("login")` shows up in Better Stack with `user_id=42` searchable as a structured field. Example payload:

```json
{
  "dt": "2026-04-29T12:34:56.789012+00:00",
  "level": "WARNING",
  "message": "slow checkout",
  "source": {
    "name": "billing.checkout",
    "function": "submit",
    "module": "checkout",
    "line": 142,
    "file": "checkout.py",
    "process": { "id": 4711, "name": "MainProcess" },
    "thread": { "id": 140523, "name": "MainThread" }
  },
  "context": { "user_id": 42, "route": "/checkout" }
}
```

## Configuration

```python
BetterStackSink(
    source_token="...",
    host="in.logs.betterstack.com",  # or your regional ingesting host
    batch_size=100,                  # max records per request
    flush_interval=5.0,              # seconds before forcing a flush
    max_queue=10_000,                # hard cap; older records drop with a stderr warning
    timeout=10.0,                    # per-request HTTP timeout (seconds)
)
```

The host you pass must match the **ingesting host** shown in your Better Stack source. Some accounts get regional hosts like `s1234567.eu-nbg-2.betterstackdata.com`; copy that exactly.

## Async / multi-process apps

The sink is safe to share across threads — it uses a thread-safe queue and a single background worker. For multi-process deployments (e.g. `gunicorn -w 4`), instantiate one sink per process; do **not** share the same instance across forks.

If your app uses `asyncio`, you can register the sink the same way — Loguru handles the dispatch, and the sink drains on its own thread:

```python
import asyncio
from loguru import logger
from loguru_betterstack import BetterStackSink

logger.add(BetterStackSink("..."), level="INFO")

async def main():
    logger.bind(request_id="abc").info("starting")
    await asyncio.sleep(0)

asyncio.run(main())
```

## Graceful shutdown

`register_atexit()` is the easiest way — the sink will flush on interpreter exit. If you control your own shutdown path, call `sink.close()` directly; it blocks until the queue is drained (or the optional `timeout` elapses).

## License

MIT. See `LICENSE`.
