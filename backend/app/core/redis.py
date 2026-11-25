from __future__ import annotations

from redis.asyncio import Redis

from app.core.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url, decode_responses=True, health_check_interval=30)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
    _redis = None


# Redis key names used across services.
GENERATION_STREAM = "stream:generations"
GENERATION_DLQ_STREAM = "stream:generations:dlq"
SCHEDULER_GROUP = "schedulers"


def agent_channel(worker_id: object) -> str:
    return f"agent:{worker_id}"


def user_channel(user_id: object) -> str:
    return f"user:{user_id}:events"


def user_event_log(user_id: object) -> str:
    return f"events:user:{user_id}"
