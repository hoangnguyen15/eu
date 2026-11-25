"""Mobile realtime channel: WS /ws/v1/events

Auth: `Authorization: Bearer <access>` header or `?access_token=` query param.
Resume: `?since=<cursor>` replays missed events from the per-user Redis stream.
Heartbeat: server sends {"event":"ping"} every HEARTBEAT seconds; clients may
send {"type":"ping"} and receive {"event":"pong"}. The socket is closed with
4401 when the access token expires so the client refreshes and reconnects.
REST (GET /api/v1/generations/{id}) is the authoritative fallback.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.config import get_settings
from app.core.db import session_factory
from app.core.deps import authenticate_token
from app.core.exceptions import UnauthorizedError
from app.core.redis import get_redis, user_channel, user_event_log
from app.core.security import decode_access_token

log = logging.getLogger(__name__)
router = APIRouter()

CLOSE_UNAUTHORIZED = 4401


def _stream_id_gt(a: str, b: str) -> bool:
    ams, aseq = (int(x) for x in a.split("-"))
    bms, bseq = (int(x) for x in b.split("-"))
    return (ams, aseq) > (bms, bseq)


@router.websocket("/ws/v1/events")
async def events_socket(ws: WebSocket, access_token: str | None = Query(None),
                        since: str | None = Query(None, max_length=64)) -> None:
    token = access_token
    auth = ws.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        token = auth[7:]
    if not token:
        await ws.close(code=CLOSE_UNAUTHORIZED)
        return
    try:
        claims = decode_access_token(token)
        async with session_factory()() as db:
            user = await authenticate_token(token, db)
    except UnauthorizedError:
        await ws.close(code=CLOSE_UNAUTHORIZED)
        return

    await ws.accept()
    redis = get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(user_channel(user.id))
    send_lock = asyncio.Lock()
    last_cursor: str | None = since

    async def send(obj: dict) -> None:
        async with send_lock:
            await ws.send_text(json.dumps(obj, default=str))

    await send({"event": "connection.ready", "data": {"user_id": str(user.id),
                                                     "heartbeat_seconds": get_settings().heartbeat_interval_seconds}})
    if since:
        try:
            entries = await redis.xrange(user_event_log(user.id), min=f"({since}", max="+", count=500)
        except Exception:
            entries = []
        for cursor, fields in entries:
            evt = json.loads(fields["e"])
            evt["cursor"] = cursor
            await send(evt)
            last_cursor = cursor

    async def forward() -> None:
        nonlocal last_cursor
        async for item in pubsub.listen():
            if item.get("type") != "message":
                continue
            evt = json.loads(item["data"])
            cur = evt.get("cursor")
            if cur and last_cursor and not _stream_id_gt(cur, last_cursor):
                continue  # already delivered by replay
            if cur:
                last_cursor = cur
            await send(evt)

    async def heartbeat() -> None:
        interval = get_settings().heartbeat_interval_seconds
        while True:
            await asyncio.sleep(interval)
            await send({"event": "ping", "timestamp": time.time()})

    async def token_expiry() -> None:
        await asyncio.sleep(max(0, int(claims["exp"]) - time.time()))
        with contextlib.suppress(Exception):
            await ws.close(code=CLOSE_UNAUTHORIZED, reason="token expired")

    async def receive() -> None:
        while True:
            raw = await ws.receive_text()
            if len(raw) > 4096:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("type") == "ping":
                await send({"event": "pong", "timestamp": time.time()})

    tasks = [asyncio.create_task(c()) for c in (forward, heartbeat, token_expiry, receive)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                log.debug("events socket closed: %r", exc)
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe()
            await pubsub.aclose()
        with contextlib.suppress(Exception):
            await ws.close()
