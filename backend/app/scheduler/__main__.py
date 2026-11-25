"""Scheduler process: `python -m app.scheduler`.

Woken by the Redis stream `stream:generations` (XREADGROUP) and by a periodic
tick; the database remains the source of truth, so missed wake-ups only add
latency, never lose jobs."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from pathlib import Path

from redis.exceptions import ResponseError

from app.core.config import get_settings
from app.core.db import dispose_engine, session_factory
from app.core.logging import configure_logging
from app.core.redis import GENERATION_STREAM, SCHEDULER_GROUP, close_redis, get_redis
from app.scheduler.dispatcher import tick

log = logging.getLogger("scheduler")
ALIVE_FILE = Path(os.environ.get("SCHEDULER_ALIVE_FILE", "/tmp/scheduler.alive"))


async def _ensure_group() -> None:
    try:
        await get_redis().xgroup_create(GENERATION_STREAM, SCHEDULER_GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def run(stop: asyncio.Event) -> None:
    s = get_settings()
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    await _ensure_group()
    redis = get_redis()
    factory = session_factory()
    log.info("scheduler started", extra={"extra_fields": {"consumer": consumer}})
    while not stop.is_set():
        try:
            stats = await tick(factory)
            ALIVE_FILE.touch()  # liveness probe for the container healthcheck
            if any(stats.values()):
                log.info("scheduler tick", extra={"extra_fields": stats})
            block_ms = int(s.scheduler_poll_interval_seconds * 1000)
            resp = await redis.xreadgroup(SCHEDULER_GROUP, consumer, {GENERATION_STREAM: ">"}, count=100,
                                          block=block_ms)
            for _stream, entries in resp or []:
                ids = [eid for eid, _ in entries]
                if ids:
                    await redis.xack(GENERATION_STREAM, SCHEDULER_GROUP, *ids)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("scheduler loop error")
            await asyncio.sleep(2)


async def main() -> None:
    configure_logging(get_settings().log_level)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await run(stop)
    finally:
        await close_redis()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
