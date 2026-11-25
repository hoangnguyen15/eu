"""Realtime user events: Redis Pub/Sub for live fan-out + a capped Redis Stream
per user so a reconnecting client can replay what it missed (`?since=<cursor>`)."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.redis import get_redis, user_channel, user_event_log

log = logging.getLogger(__name__)

EVENT_LOG_MAXLEN = 500


def build_event(event: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "event": event,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": data,
    }


async def publish_user_event(user_id: uuid.UUID | str, event: str, data: dict[str, Any]) -> dict[str, Any]:
    """Must be called *after* the DB transaction that produced the state change
    has committed, so clients never observe an event for uncommitted state."""
    payload = build_event(event, data)
    redis = get_redis()
    try:
        cursor = await redis.xadd(user_event_log(user_id), {"e": json.dumps(payload, default=str)},
                                  maxlen=EVENT_LOG_MAXLEN, approximate=True)
        payload["cursor"] = cursor
        await redis.publish(user_channel(user_id), json.dumps(payload, default=str))
    except Exception:  # pragma: no cover - realtime is best effort; REST is the source of truth
        log.exception("failed to publish user event", extra={"extra_fields": {"event": event}})
    return payload


def generation_event_data(gen: Any, **extra: Any) -> dict[str, Any]:
    data = {
        "generation_id": str(gen.id),
        "status": gen.status.lower(),
        "progress": gen.progress,
        "error_code": gen.error_code,
        "error_message": gen.error_message,
    }
    data.update(extra)
    return data
