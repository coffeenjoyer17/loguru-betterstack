"""Minimal example: ship Loguru logs to Better Stack.

Run with:

    SOURCE_TOKEN=... INGEST_HOST=in.logs.betterstack.com python examples/basic.py
"""

import os
import time

from loguru import logger

from loguru_betterstack import BetterStackSink


def main() -> None:
    token = os.environ.get("SOURCE_TOKEN")
    if not token:
        raise SystemExit("Set SOURCE_TOKEN to your Better Stack source token")

    sink = BetterStackSink(
        source_token=token,
        host=os.environ.get("INGEST_HOST", "in.logs.betterstack.com"),
        batch_size=20,
        flush_interval=2.0,
    )
    sink.register_atexit()

    logger.add(sink, level="DEBUG")

    logger.info("hello from loguru-betterstack")
    logger.bind(request_id="abc-123", route="/checkout").warning("slow checkout")
    logger.bind(user_id=42).info("user upgraded to pro")

    try:
        _ = 1 / 0
    except ZeroDivisionError:
        logger.exception("caught a divide-by-zero, traceback should ship")

    # Give the background thread a moment, but `register_atexit` will also flush.
    time.sleep(3)
    print("done")


if __name__ == "__main__":
    main()
