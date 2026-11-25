"""Agent WebSocket gateway (`/ws/v1/agent`).

TLS client authentication is performed by the mTLS reverse proxy, which
forwards the verified certificate. The backend then:
  1. requires the proxy's shared secret (anti header-spoofing),
  2. requires X-Client-Verify == SUCCESS,
  3. pins the certificate's SHA-256 fingerprint to an enabled workstation.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.db import session_factory
from app.core.redis import GENERATION_STREAM, agent_channel, get_redis
from app.core.security import constant_time_equals
from app.models import Generation, LeaseStatus, TaskLease, WorkerEvent, Workstation, WorkstationStatus
from app.scheduler.offers import build_offer
from app.services.audit import audit
from app.workers import handlers as h
from app.workers import pki
from app.workers import protocol as p

log = logging.getLogger(__name__)
router = APIRouter()

CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_SUPERSEDED = 4409


def _now() -> datetime:
    return datetime.now(UTC)


async def authenticate_agent(ws: WebSocket) -> uuid.UUID | None:
    s = get_settings()
    secret = ws.headers.get("x-gateway-secret", "")
    if not secret or not constant_time_equals(secret, s.gateway_shared_secret):
        return None
    if ws.headers.get("x-client-verify") != "SUCCESS":
        return None
    raw = ws.headers.get("x-client-cert")
    if not raw:
        return None
    try:
        cert = pki.parse_forwarded_cert(raw)
        ws_id = pki.cert_workstation_id(cert)
    except pki.PkiError:
        return None
    if cert.not_valid_after_utc <= _now():
        return None
    fp = pki.fingerprint(cert)
    async with session_factory()() as db:
        row = (await db.execute(select(Workstation).where(Workstation.id == ws_id))).scalar_one_or_none()
        if row is None or row.status == WorkstationStatus.DISABLED.value or row.cert_fingerprint != fp:
            return None
    return ws_id


class AgentConnection:
    def __init__(self, ws: WebSocket, worker_id: uuid.UUID) -> None:
        self.ws = ws
        self.worker_id = worker_id
        self.conn_id = uuid.uuid4().hex
        self.connected_at = _now()
        self._send_lock = asyncio.Lock()

    async def send(self, msg: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_text(json.dumps(msg, default=str))

    async def ack(self, event_id: str | None, status: str, result: dict[str, Any] | None = None, *,
                  duplicate: bool = False, retryable: bool = False) -> None:
        await self.send(p.message(p.MsgType.EVENT_ACK, {
            "event_id": event_id, "status": status, "result": result or {}, "duplicate": duplicate,
            "retryable": retryable,
        }))


@router.websocket("/ws/v1/agent")
async def agent_socket(ws: WebSocket) -> None:
    worker_id = await authenticate_agent(ws)
    if worker_id is None:
        await ws.close(code=CLOSE_UNAUTHORIZED)
        return
    await ws.accept()
    conn = AgentConnection(ws, worker_id)
    redis = get_redis()
    # Single active connection per workstation: newer connection supersedes older.
    await redis.set(f"agent:conn:{worker_id}", conn.conn_id, ex=3600)
    await redis.publish(agent_channel(worker_id), json.dumps({"type": "_SUPERSEDED", "conn_id": conn.conn_id}))

    pubsub = redis.pubsub()
    await pubsub.subscribe(agent_channel(worker_id))
    forward = asyncio.create_task(_forward_server_messages(conn, pubsub))
    log.info("agent connected", extra={"extra_fields": {"worker_id": str(worker_id)}})
    try:
        while True:
            raw = await ws.receive_text()
            await _dispatch(conn, raw)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("agent connection error")
    finally:
        forward.cancel()
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe()
            await pubsub.aclose()
        await _on_disconnect(conn)


async def _forward_server_messages(conn: AgentConnection, pubsub: Any) -> None:
    try:
        async for item in pubsub.listen():
            if item.get("type") != "message":
                continue
            msg = json.loads(item["data"])
            if msg.get("type") == "_SUPERSEDED":
                if msg.get("conn_id") != conn.conn_id:
                    with contextlib.suppress(Exception):
                        await conn.ws.close(code=CLOSE_SUPERSEDED)
                    return
                continue
            await conn.send(msg)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("agent forwarder stopped")
        with contextlib.suppress(Exception):
            await conn.ws.close(code=1011)


async def _on_disconnect(conn: AgentConnection) -> None:
    redis = get_redis()
    with contextlib.suppress(Exception):
        current = await redis.get(f"agent:conn:{conn.worker_id}")
        if current != conn.conn_id:
            return  # superseded; the newer connection owns the status
        await redis.delete(f"agent:conn:{conn.worker_id}")
    async with session_factory()() as db:
        await db.execute(
            update(Workstation)
            .where(Workstation.id == conn.worker_id, Workstation.status == WorkstationStatus.ONLINE.value)
            .values(status=WorkstationStatus.OFFLINE.value)
        )
        await db.commit()
    log.info("agent disconnected", extra={"extra_fields": {"worker_id": str(conn.worker_id)}})


async def _dispatch(conn: AgentConnection, raw: str) -> None:
    if len(raw) > 256 * 1024:
        await conn.send(p.message(p.MsgType.ERROR, {"reason": "MESSAGE_TOO_LARGE"}))
        return
    try:
        env = p.Envelope.model_validate_json(raw)
    except ValidationError as exc:
        await conn.send(p.message(p.MsgType.ERROR, {"reason": "INVALID_MESSAGE", "detail": exc.errors()[:3]}))
        return

    if env.type == p.MsgType.REGISTER:
        await _handle_register(conn, env)
    elif env.type == p.MsgType.HEARTBEAT:
        await _handle_heartbeat(conn, env)
    elif env.type in p.PERSISTED_EVENTS:
        await _handle_persisted(conn, env)
    else:
        await conn.send(p.message(p.MsgType.ERROR, {"reason": "UNSUPPORTED_TYPE", "type": env.type.value}))


async def _handle_register(conn: AgentConnection, env: p.Envelope) -> None:
    payload = p.RegisterPayload.model_validate(env.payload)
    s = get_settings()
    async with session_factory()() as db:
        ws_row = (await db.execute(select(Workstation).where(Workstation.id == conn.worker_id)
                                   .with_for_update())).scalar_one()
        ws_row.hostname = payload.hostname or ws_row.hostname
        ws_row.agent_version = payload.agent_version
        ws_row.capacity = payload.capacity
        ws_row.providers = payload.providers
        ws_row.status = WorkstationStatus.ONLINE.value
        ws_row.last_heartbeat_at = _now()
        ws_row.connected_at = conn.connected_at
        audit(db, action="workstation.register", resource_type="workstation", resource_id=conn.worker_id,
              actor_id=conn.worker_id, actor_type="worker",
              details={"version": payload.agent_version, "capacity": payload.capacity})
        await db.commit()

        # Re-send still-valid offers that may have been lost while disconnected.
        rows = (await db.execute(
            select(TaskLease, Generation).join(Generation, Generation.id == TaskLease.generation_id)
            .where(TaskLease.worker_id == conn.worker_id, TaskLease.status == LeaseStatus.OFFERED.value,
                   TaskLease.expires_at > _now())
        )).all()
        offers = [await build_offer(db, lease, gen) for lease, gen in rows]
    await conn.ack(env.payload.get("event_id") or env.id, "ok", {
        "workstation_id": str(conn.worker_id),
        "heartbeat_interval_seconds": s.heartbeat_interval_seconds,
        "server_time": _now().isoformat(),
    })
    for offer in offers:
        await conn.send(offer)
    await wake_scheduler("worker_registered")  # new capacity is available right away


async def _handle_heartbeat(conn: AgentConnection, env: p.Envelope) -> None:
    async with session_factory()() as db:
        await db.execute(
            update(Workstation)
            .where(Workstation.id == conn.worker_id, Workstation.status != WorkstationStatus.DISABLED.value)
            .values(last_heartbeat_at=_now(), status=WorkstationStatus.ONLINE.value)
        )
        await db.commit()
    await conn.ack(env.payload.get("event_id") or env.id, "ok", {"server_time": _now().isoformat()})


_PAYLOADS: dict[p.MsgType, type] = {
    p.MsgType.TASK_ACCEPT: p.TaskRef,
    p.MsgType.TASK_REJECT: p.TaskRejectPayload,
    p.MsgType.TASK_STARTED: p.TaskRef,
    p.MsgType.TASK_PROGRESS: p.TaskProgressPayload,
    p.MsgType.TASK_COMPLETED: p.TaskCompletedPayload,
    p.MsgType.TASK_FAILED: p.TaskFailedPayload,
    p.MsgType.UPLOAD_REQUEST: p.UploadRequestPayload,
    p.MsgType.UPLOAD_COMPLETED: p.UploadCompletedPayload,
    p.MsgType.SESSION_STATUS: p.SessionStatusPayload,
}

_HANDLERS: dict[p.MsgType, Callable[..., Awaitable[h.Outcome]]] = {
    p.MsgType.TASK_ACCEPT: h.handle_task_accept,
    p.MsgType.TASK_REJECT: h.handle_task_reject,
    p.MsgType.TASK_STARTED: h.handle_task_started,
    p.MsgType.TASK_PROGRESS: h.handle_task_progress,
    p.MsgType.TASK_COMPLETED: h.handle_task_completed,
    p.MsgType.TASK_FAILED: h.handle_task_failed,
    p.MsgType.UPLOAD_REQUEST: h.handle_upload_request,
    p.MsgType.SESSION_STATUS: h.handle_session_status,
}


async def process_event(worker_id: uuid.UUID, env: p.Envelope) -> tuple[str, dict[str, Any], bool]:
    """Validates, dedups and applies one persisted agent event.
    Returns (status, result, duplicate). Raises on unexpected errors."""
    event_id = str(env.payload.get("event_id") or "")
    try:
        payload = _PAYLOADS[env.type].model_validate(env.payload)
    except ValidationError as exc:
        return "rejected", {"reason": "INVALID_PAYLOAD", "detail": json.loads(exc.json())[:3]}, False

    factory = session_factory()
    async with factory() as db:
        prior = (await db.execute(select(WorkerEvent).where(WorkerEvent.worker_id == worker_id,
                                                            WorkerEvent.event_id == event_id))).scalar_one_or_none()
        if prior is not None:
            res = prior.result or {}
            return res.get("_status", "ok"), {k: v for k, v in res.items() if k != "_status"}, True

    verified: list[h.VerifiedFile] = []
    verify_error: str | None = None
    if env.type == p.MsgType.UPLOAD_COMPLETED:
        async with factory() as db:
            lease = (await db.execute(select(TaskLease).where(TaskLease.id == payload.task_id,
                                                              TaskLease.worker_id == worker_id))).scalar_one_or_none()
            issued = list(lease.upload_keys) if lease is not None else []
        if lease is not None and lease.status == LeaseStatus.UPLOADING.value:
            verified, verify_error = await h.verify_uploaded_files(issued, payload.files)

    async with factory() as db:
        if env.type == p.MsgType.UPLOAD_COMPLETED:
            outcome = await h.handle_upload_completed(db, worker_id, payload, verified, verify_error)
        else:
            outcome = await _HANDLERS[env.type](db, worker_id, payload)
        db.add(WorkerEvent(worker_id=worker_id, event_id=event_id, event_type=env.type.value,
                           payload=_redact(env.payload), result={"_status": outcome.status, **outcome.result}))
        try:
            await db.commit()
        except IntegrityError:
            # The same event was processed concurrently on another connection.
            await db.rollback()
            prior = (await db.execute(select(WorkerEvent).where(
                WorkerEvent.worker_id == worker_id, WorkerEvent.event_id == event_id))).scalar_one()
            res = prior.result or {}
            return res.get("_status", "ok"), {k: v for k, v in res.items() if k != "_status"}, True

    for fn in outcome.after_commit:
        try:
            await fn()
        except Exception:
            log.exception("post-commit side effect failed")
    if env.type in _SLOT_RELEASING:
        await wake_scheduler("slot_released")
    return outcome.status, outcome.result, False


# Events after which a worker slot may be free: wake the scheduler immediately
# instead of waiting for its next poll tick (removes ~1 s idle gap per job).
_SLOT_RELEASING = {p.MsgType.TASK_COMPLETED, p.MsgType.TASK_FAILED, p.MsgType.TASK_REJECT,
                   p.MsgType.UPLOAD_COMPLETED}


async def wake_scheduler(reason: str) -> None:
    try:
        await get_redis().xadd(GENERATION_STREAM, {"wake": reason}, maxlen=100_000, approximate=True)
    except Exception:
        log.debug("scheduler wake-up failed", exc_info=True)


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if "error_message" in out and isinstance(out["error_message"], str):
        out["error_message"] = out["error_message"][:500]
    return out


async def _handle_persisted(conn: AgentConnection, env: p.Envelope) -> None:
    event_id = env.payload.get("event_id")
    if not event_id:
        await conn.send(p.message(p.MsgType.ERROR, {"reason": "MISSING_EVENT_ID", "type": env.type.value}))
        return
    try:
        status, result, duplicate = await process_event(conn.worker_id, env)
    except Exception:
        log.exception("agent event processing failed",
                      extra={"extra_fields": {"type": env.type.value, "event_id": event_id}})
        await conn.ack(event_id, "error", {"reason": "INTERNAL_ERROR"}, retryable=True)
        return
    await conn.ack(event_id, status, result, duplicate=duplicate)
