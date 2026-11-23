"""Celery entrypoint. Milestone 1 ships only a liveness task; job processing arrives in M3."""

import os

from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

app = Celery("worker", broker=REDIS_URL, backend=None)
app.conf.update(
    task_acks_late=True,  # a crashed worker must not lose a task (spec §14)
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 3600},
)


@app.task(name="worker.ping")
def ping() -> str:
    return "pong"
