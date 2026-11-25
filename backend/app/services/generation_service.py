"""Generation lifecycle: create (idempotent, with credit reservation), query,
cancel, and terminal-state transitions shared by the gateway and scheduler."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import CurrentUser
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.redis import GENERATION_STREAM, agent_channel, get_redis
from app.models import (
    TERMINAL_GENERATION_STATUSES,
    Asset,
    CreditState,
    Generation,
    GenerationMetadata,
    GenerationStatus,
    LeaseStatus,
    TaskLease,
)
from app.providers import registry
from app.schemas import GenerationCreate
from app.services import credit_service
from app.services.audit import audit
from app.services.events import generation_event_data, publish_user_event
from app.storage import s3

log = logging.getLogger(__name__)

FAILURE_EVENT = "generation.failed"
STATUS_EVENTS = {
    GenerationStatus.QUEUED.value: "generation.queued",
    GenerationStatus.RUNNING.value: "generation.started",
    GenerationStatus.UPLOADING.value: "generation.progress",
    GenerationStatus.DISPATCHED.value: "generation.progress",
    GenerationStatus.COMPLETED.value: "generation.completed",
    GenerationStatus.CANCELLED.value: "generation.cancelled",
    GenerationStatus.FAILED.value: FAILURE_EVENT,
    GenerationStatus.AUTH_REQUIRED.value: FAILURE_EVENT,
    GenerationStatus.PROVIDER_LIMITED.value: FAILURE_EVENT,
    GenerationStatus.REVIEW_REQUIRED.value: FAILURE_EVENT,
}


def _request_hash(req: GenerationCreate, provider: str) -> str:
    canonical = json.dumps({
        "prompt": req.prompt, "aspect_ratio": req.aspect_ratio, "n": req.number_of_images,
        "refs": sorted(str(r) for r in req.reference_images), "provider": provider, "metadata": req.metadata,
    }, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate(req: GenerationCreate) -> str:
    s = get_settings()
    provider = req.provider or s.default_provider
    if not registry.is_enabled(provider):
        raise AppError(f"Provider '{provider}' is not enabled", code="PROVIDER_DISABLED", status_code=400)
    if req.aspect_ratio not in s.allowed_aspect_ratios:
        raise AppError("Unsupported aspect_ratio", code="VALIDATION_ERROR", status_code=422,
                       details={"allowed": s.allowed_aspect_ratios})
    if req.number_of_images > s.max_images_per_generation:
        raise AppError("Too many images requested", code="VALIDATION_ERROR", status_code=422,
                       details={"max": s.max_images_per_generation})
    if len(req.prompt) > s.max_prompt_length:
        raise AppError("Prompt too long", code="VALIDATION_ERROR", status_code=422)
    return provider


async def create_generation(db: AsyncSession, user: CurrentUser, req: GenerationCreate,
                            idempotency_key: str | None, ip: str | None) -> tuple[Generation, bool]:
    """Returns (generation, created). `created` is False for an idempotent replay."""
    provider = _validate(req)
    req_hash = _request_hash(req, provider)

    if idempotency_key:
        existing = await _find_by_idempotency_key(db, user.id, idempotency_key)
        if existing is not None:
            return _check_replay(existing, req_hash), False

    if req.reference_images:
        owned = (await db.execute(
            select(Asset.id).where(Asset.id.in_(req.reference_images), Asset.user_id == user.id)
        )).scalars().all()
        if len(set(owned)) != len(set(req.reference_images)):
            raise NotFoundError("Reference image not found", code="REFERENCE_NOT_FOUND")

    gen = Generation(
        id=uuid.uuid4(),
        user_id=user.id,
        provider=provider,
        prompt=req.prompt,
        aspect_ratio=req.aspect_ratio,
        number_of_images=req.number_of_images,
        reference_asset_ids=[str(r) for r in req.reference_images],
        client_metadata=req.metadata,
        status=GenerationStatus.QUEUED.value,
        idempotency_key=idempotency_key,
        request_hash=req_hash,
        credit_cost=req.number_of_images * get_settings().credit_cost_per_image,
        credit_state=CreditState.NONE.value,
    )
    db.add(gen)
    try:
        await db.flush()
    except IntegrityError:
        # Concurrent request with the same Idempotency-Key won the race.
        await db.rollback()
        existing = await _find_by_idempotency_key(db, user.id, idempotency_key or "")
        if existing is None:
            raise
        return _check_replay(existing, req_hash), False

    wallet = await credit_service.reserve(db, gen)  # raises 402 and the tx is rolled back by caller
    audit(db, action="generation.create", resource_type="generation", resource_id=gen.id, actor_id=user.id, ip=ip,
          details={"provider": provider, "n": gen.number_of_images, "cost": gen.credit_cost})
    await db.commit()
    await db.refresh(gen)

    try:
        await get_redis().xadd(GENERATION_STREAM, {"generation_id": str(gen.id)}, maxlen=100_000, approximate=True)
    except Exception:  # the scheduler also polls the DB, so a lost wake-up only adds latency
        log.warning("failed to enqueue generation wake-up", exc_info=True)
    await publish_user_event(user.id, "generation.queued", generation_event_data(gen))
    await publish_user_event(user.id, "credits.updated", credit_service.wallet_view(wallet))
    return gen, True


async def _find_by_idempotency_key(db: AsyncSession, user_id: uuid.UUID, key: str) -> Generation | None:
    stmt = select(Generation).where(Generation.user_id == user_id, Generation.idempotency_key == key)
    return (await db.execute(stmt)).scalar_one_or_none()


def _check_replay(existing: Generation, req_hash: str) -> Generation:
    if existing.request_hash != req_hash:
        raise ConflictError("Idempotency-Key was already used with a different request body",
                            code="IDEMPOTENCY_KEY_REUSED")
    return existing


# ------------------------------------------------------------------ reads ----
async def get_owned_generation(db: AsyncSession, user: CurrentUser, generation_id: uuid.UUID) -> Generation:
    gen = await db.get(Generation, generation_id)
    # Same 404 for "missing" and "someone else's" to avoid leaking existence.
    if gen is None or gen.user_id != user.id:
        raise NotFoundError("Generation not found")
    return gen


def _encode_cursor(created_at: datetime, gid: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{gid}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts, gid = raw.split("|", 1)
        return datetime.fromisoformat(ts), uuid.UUID(gid)
    except Exception as exc:
        raise AppError("Invalid cursor", code="VALIDATION_ERROR", status_code=422) from exc


async def list_generations(db: AsyncSession, user: CurrentUser, *, limit: int, cursor: str | None,
                           status: str | None) -> tuple[list[Generation], str | None]:
    stmt = select(Generation).where(Generation.user_id == user.id)
    if status:
        stmt = stmt.where(Generation.status == status.upper())
    if cursor:
        ts, gid = _decode_cursor(cursor)
        stmt = stmt.where(or_(Generation.created_at < ts, and_(Generation.created_at == ts, Generation.id < gid)))
    stmt = stmt.order_by(Generation.created_at.desc(), Generation.id.desc()).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].id)
    return rows, next_cursor


def asset_view(asset: Asset, *, signed: bool = True) -> dict[str, Any]:
    s = get_settings()
    view: dict[str, Any] = {
        "id": asset.id, "generation_id": asset.generation_id, "image_index": asset.image_index,
        "mime_type": asset.mime_type, "width": asset.width, "height": asset.height, "file_size": asset.file_size,
        "sha256": asset.sha256, "created_at": asset.created_at, "metadata": asset.metadata_json,
    }
    if signed:
        view["url"] = s3.presign_get(asset.storage_key)
        view["thumbnail_url"] = s3.presign_get(asset.thumbnail_key) if asset.thumbnail_key else None
        view["url_expires_at"] = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() + s.signed_url_ttl_seconds, UTC)
    return view


async def generation_view(db: AsyncSession, gen: Generation, *, with_assets: bool = True) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": gen.id, "status": gen.status.lower(), "prompt": gen.prompt, "aspect_ratio": gen.aspect_ratio,
        "number_of_images": gen.number_of_images, "provider": gen.provider, "progress": gen.progress,
        "error_code": gen.error_code, "error_message": gen.error_message, "credit_cost": gen.credit_cost,
        "credit_state": gen.credit_state.lower(), "created_at": gen.created_at, "started_at": gen.started_at,
        "completed_at": gen.completed_at, "assets": [], "provider_metadata": None,
    }
    if with_assets and gen.status == GenerationStatus.COMPLETED.value:
        assets = (await db.execute(
            select(Asset).where(Asset.generation_id == gen.id).order_by(Asset.image_index)
        )).scalars().all()
        view["assets"] = [asset_view(a) for a in assets]
        meta = (await db.execute(
            select(GenerationMetadata).where(GenerationMetadata.generation_id == gen.id)
        )).scalar_one_or_none()
        if meta:
            view["provider_metadata"] = {"provider": meta.provider, "model": meta.model, **meta.provider_metadata}
    return view


# ----------------------------------------------------------- transitions ----
async def lock_generation(db: AsyncSession, generation_id: uuid.UUID) -> Generation | None:
    stmt = select(Generation).where(Generation.id == generation_id).with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def cancel_generation(db: AsyncSession, user: CurrentUser, generation_id: uuid.UUID,
                            ip: str | None) -> Generation:
    gen = await lock_generation(db, generation_id)
    if gen is None or gen.user_id != user.id:
        raise NotFoundError("Generation not found")
    if gen.status == GenerationStatus.CANCELLED.value:
        return gen  # idempotent
    if gen.status in {s.value for s in TERMINAL_GENERATION_STATUSES}:
        raise ConflictError(f"Generation already {gen.status}", code="INVALID_STATE")

    revoked_lease: TaskLease | None = None
    if gen.status == GenerationStatus.DISPATCHED.value:
        lease = (await db.execute(
            select(TaskLease).where(TaskLease.generation_id == gen.id,
                                    TaskLease.status.in_([LeaseStatus.OFFERED.value, LeaseStatus.ACCEPTED.value]))
            .with_for_update()
        )).scalar_one_or_none()
        if lease is not None and lease.status == LeaseStatus.ACCEPTED.value:
            raise ConflictError("Generation is already being processed by a worker", code="INVALID_STATE")
        if lease is not None:
            lease.status = LeaseStatus.CANCELLED.value
            lease.finished_at = datetime.now(UTC)
            revoked_lease = lease
    elif gen.status != GenerationStatus.QUEUED.value:
        # RUNNING/UPLOADING: the provider may already be producing output; we do
        # not pretend we can stop it. The client can wait for the outcome.
        raise ConflictError("Generation is already running and can no longer be cancelled", code="INVALID_STATE")

    gen.status = GenerationStatus.CANCELLED.value
    gen.completed_at = datetime.now(UTC)
    gen.error_code = "CANCELLED_BY_USER"
    wallet = await credit_service.release(db, gen, note="cancelled")
    audit(db, action="generation.cancel", resource_type="generation", resource_id=gen.id, actor_id=user.id, ip=ip)
    await db.commit()

    if revoked_lease is not None:
        await notify_agent(revoked_lease.worker_id, "TASK_CANCEL",
                           {"task_id": str(revoked_lease.id), "generation_id": str(gen.id)})
    await publish_user_event(gen.user_id, "generation.cancelled", generation_event_data(gen))
    if wallet is not None:
        await publish_user_event(gen.user_id, "credits.updated", credit_service.wallet_view(wallet))
    return gen


def status_for_error(error_code: str | None) -> GenerationStatus:
    if error_code == "AUTH_REQUIRED":
        return GenerationStatus.AUTH_REQUIRED
    if error_code == "PROVIDER_LIMITED":
        return GenerationStatus.PROVIDER_LIMITED
    if error_code in ("REVIEW_REQUIRED", "OUTCOME_UNKNOWN"):
        return GenerationStatus.REVIEW_REQUIRED
    return GenerationStatus.FAILED


async def finalize_failure(db: AsyncSession, gen: Generation, *, error_code: str, error_message: str,
                           status: GenerationStatus | None = None) -> dict[str, Any] | None:
    """Mark a *locked* generation as failed and release credits if refundable.
    Caller commits, then calls `emit_terminal_events`."""
    gen.status = (status or status_for_error(error_code)).value
    gen.error_code = error_code
    gen.error_message = (error_message or "")[:2000]
    gen.completed_at = datetime.now(UTC)
    if gen.status == GenerationStatus.REVIEW_REQUIRED.value:
        # Outcome unclear: keep credits reserved until an admin resolves it
        # (POST /api/v1/admin/generations/{id}/resolve).
        return None
    if error_code in get_settings().non_refundable_error_codes:
        wallet = await credit_service.settle(db, gen)
    else:
        wallet = await credit_service.release(db, gen, note=error_code)
    return credit_service.wallet_view(wallet) if wallet is not None else None


async def emit_terminal_events(gen: Generation, wallet_view: dict[str, Any] | None,
                               extra: dict[str, Any] | None = None) -> None:
    event = STATUS_EVENTS.get(gen.status, FAILURE_EVENT)
    await publish_user_event(gen.user_id, event, generation_event_data(gen, **(extra or {})))
    if wallet_view is not None:
        await publish_user_event(gen.user_id, "credits.updated", wallet_view)


async def notify_agent(worker_id: uuid.UUID, msg_type: str, payload: dict[str, Any]) -> None:
    message = {"type": msg_type, "id": uuid.uuid4().hex, "ts": datetime.now(UTC).isoformat(), "payload": payload}
    try:
        await get_redis().publish(agent_channel(worker_id), json.dumps(message, default=str))
    except Exception:
        log.warning("failed to publish agent message", exc_info=True)
