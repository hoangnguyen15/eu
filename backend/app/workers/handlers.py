"""Business handling of agent events. Every handler runs inside one DB
transaction together with the `worker_events` dedup row, and returns
post-commit side effects (realtime notifications) to run after COMMIT.

Lock order: generation -> task_lease -> wallet (same as scheduler / cancel).
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    AccountSession,
    AccountStatus,
    Asset,
    AuthorizationStatus,
    Generation,
    GenerationMetadata,
    GenerationStatus,
    GoogleAccount,
    LeaseStatus,
    SessionStatus,
    TaskLease,
)
from app.services import credit_service, generation_service
from app.services.audit import audit
from app.services.events import generation_event_data, publish_user_event
from app.storage import images, s3
from app.workers import protocol as p

log = logging.getLogger(__name__)

AfterCommit = Callable[[], Awaitable[None]]


@dataclass
class Outcome:
    status: str = "ok"  # ok | rejected
    result: dict[str, Any] = field(default_factory=dict)
    after_commit: list[AfterCommit] = field(default_factory=list)


def rejected(reason: str, **extra: Any) -> Outcome:
    return Outcome(status="rejected", result={"reason": reason, **extra})


def _now() -> datetime:
    return datetime.now(UTC)


async def _load_task(db: AsyncSession, worker_id: uuid.UUID, task_id: uuid.UUID
                     ) -> tuple[Generation, TaskLease] | None:
    """Locks generation then lease. Returns None if the lease is unknown or
    belongs to another worker."""
    gen_id = (await db.execute(
        select(TaskLease.generation_id).where(TaskLease.id == task_id, TaskLease.worker_id == worker_id)
    )).scalar_one_or_none()
    if gen_id is None:
        return None
    gen = await generation_service.lock_generation(db, gen_id)
    lease = (await db.execute(select(TaskLease).where(TaskLease.id == task_id).with_for_update())).scalar_one()
    assert gen is not None
    return gen, lease


def _notify(gen: Generation, event: str, **extra: Any) -> AfterCommit:
    data = generation_event_data(gen, **extra)
    user_id = gen.user_id

    async def _run() -> None:
        await publish_user_event(user_id, event, data)
    return _run


# ------------------------------------------------------------- sessions ----
async def handle_session_status(db: AsyncSession, worker_id: uuid.UUID,
                                payload: p.SessionStatusPayload) -> Outcome:
    accepted, unknown = [], []
    touched_accounts: set[uuid.UUID] = set()
    for rep in payload.sessions:
        status = rep.status.upper()
        if status not in {s.value for s in SessionStatus}:
            status = SessionStatus.UNKNOWN.value
        acct = (await db.execute(
            select(GoogleAccount).where(GoogleAccount.google_email == rep.google_email.lower())
        )).scalar_one_or_none()
        if acct is None or acct.authorization_status != AuthorizationStatus.AUTHORIZED.value:
            # Never auto-register accounts: an admin must attest authorized use first.
            unknown.append(rep.google_email)
            continue
        sess = (await db.execute(
            select(AccountSession).where(AccountSession.account_id == acct.id,
                                         AccountSession.workstation_id == worker_id).with_for_update()
        )).scalar_one_or_none()
        if sess is None:
            sess = AccountSession(account_id=acct.id, workstation_id=worker_id, profile_ref=rep.profile_ref,
                                  provider=rep.provider, status=status)
            db.add(sess)
        sess.profile_ref = rep.profile_ref
        sess.provider = rep.provider
        sess.status = status
        sess.last_verified_at = _now()
        touched_accounts.add(acct.id)
        accepted.append(rep.google_email)
    await db.flush()
    for acct_id in touched_accounts:
        await refresh_account_session_status(db, acct_id)
    return Outcome(result={"accepted": accepted, "unregistered": unknown})


async def refresh_account_session_status(db: AsyncSession, account_id: uuid.UUID) -> None:
    statuses = set((await db.execute(
        select(AccountSession.status).where(AccountSession.account_id == account_id)
    )).scalars().all())
    acct = await db.get(GoogleAccount, account_id)
    if acct is None:
        return
    if SessionStatus.ACTIVE.value in statuses:
        acct.session_status = SessionStatus.ACTIVE.value
    elif SessionStatus.AUTH_REQUIRED.value in statuses:
        acct.session_status = SessionStatus.AUTH_REQUIRED.value
    elif statuses:
        acct.session_status = sorted(statuses)[0]
    else:
        acct.session_status = SessionStatus.UNKNOWN.value


# ---------------------------------------------------------------- tasks ----
async def handle_task_accept(db: AsyncSession, worker_id: uuid.UUID, payload: p.TaskRef) -> Outcome:
    loaded = await _load_task(db, worker_id, payload.task_id)
    if loaded is None:
        return rejected("UNKNOWN_TASK")
    gen, lease = loaded
    if lease.status != LeaseStatus.OFFERED.value:
        return rejected("LEASE_NOT_OFFERED", lease_status=lease.status)
    if lease.expires_at <= _now():
        return rejected("OFFER_EXPIRED")
    if gen.status != GenerationStatus.DISPATCHED.value:
        return rejected("GENERATION_NOT_DISPATCHED", generation_status=gen.status)
    lease.status = LeaseStatus.ACCEPTED.value
    lease.accepted_at = _now()
    lease.expires_at = _now() + timedelta(seconds=get_settings().job_timeout_seconds)
    return Outcome(result={"lease_expires_at": lease.expires_at.isoformat()})


async def handle_task_reject(db: AsyncSession, worker_id: uuid.UUID, payload: p.TaskRejectPayload) -> Outcome:
    loaded = await _load_task(db, worker_id, payload.task_id)
    if loaded is None:
        return rejected("UNKNOWN_TASK")
    gen, lease = loaded
    if lease.status != LeaseStatus.OFFERED.value:
        return rejected("LEASE_NOT_OFFERED", lease_status=lease.status)
    lease.status = LeaseStatus.REJECTED.value
    lease.finished_at = _now()
    after = await requeue_or_dead_letter(db, gen, reason=f"rejected: {payload.reason or 'n/a'}")
    return Outcome(after_commit=after)


async def requeue_or_dead_letter(db: AsyncSession, gen: Generation, *, reason: str) -> list[AfterCommit]:
    """Safe only when the provider was never invoked (lease was merely OFFERED)."""
    if gen.status != GenerationStatus.DISPATCHED.value:
        return []
    if gen.dispatch_attempts >= get_settings().max_dispatch_attempts:
        wallet = await generation_service.finalize_failure(
            db, gen, error_code="DEAD_LETTERED", error_message=f"Dispatch failed too often ({reason})",
            status=GenerationStatus.FAILED)
        audit(db, action="generation.dead_lettered", resource_type="generation", resource_id=gen.id,
              actor_type="system", details={"reason": reason, "attempts": gen.dispatch_attempts})
        gid, attempts = str(gen.id), gen.dispatch_attempts

        async def _dlq() -> None:
            from app.core.redis import GENERATION_DLQ_STREAM, get_redis
            await get_redis().xadd(GENERATION_DLQ_STREAM, {"generation_id": gid, "reason": reason,
                                                           "attempts": str(attempts)}, maxlen=100_000)
            await generation_service.emit_terminal_events(gen, wallet)
        return [_dlq]
    gen.status = GenerationStatus.QUEUED.value
    gen.worker_id = None
    gen.account_id = None
    return [_wake_scheduler(gen.id)]


def _wake_scheduler(generation_id: uuid.UUID) -> AfterCommit:
    async def _run() -> None:
        from app.core.redis import GENERATION_STREAM, get_redis
        await get_redis().xadd(GENERATION_STREAM, {"generation_id": str(generation_id)}, maxlen=100_000,
                               approximate=True)
    return _run


async def handle_task_started(db: AsyncSession, worker_id: uuid.UUID, payload: p.TaskRef) -> Outcome:
    loaded = await _load_task(db, worker_id, payload.task_id)
    if loaded is None:
        return rejected("UNKNOWN_TASK")
    gen, lease = loaded
    if lease.status == LeaseStatus.RUNNING.value:
        return Outcome()
    if lease.status != LeaseStatus.ACCEPTED.value:
        return rejected("LEASE_NOT_ACCEPTED", lease_status=lease.status)
    lease.status = LeaseStatus.RUNNING.value
    gen.status = GenerationStatus.RUNNING.value
    gen.started_at = _now()
    return Outcome(after_commit=[_notify(gen, "generation.started")])


async def handle_task_progress(db: AsyncSession, worker_id: uuid.UUID, payload: p.TaskProgressPayload) -> Outcome:
    loaded = await _load_task(db, worker_id, payload.task_id)
    if loaded is None:
        return rejected("UNKNOWN_TASK")
    gen, lease = loaded
    if lease.status not in (LeaseStatus.RUNNING.value, LeaseStatus.UPLOADING.value):
        return rejected("LEASE_NOT_RUNNING", lease_status=lease.status)
    gen.progress = max(gen.progress, min(payload.progress, 99))  # 100 only once verified + completed
    return Outcome(after_commit=[_notify(gen, "generation.progress")])


async def handle_upload_request(db: AsyncSession, worker_id: uuid.UUID,
                                payload: p.UploadRequestPayload) -> Outcome:
    s = get_settings()
    loaded = await _load_task(db, worker_id, payload.task_id)
    if loaded is None:
        return rejected("UNKNOWN_TASK")
    gen, lease = loaded
    if lease.status not in (LeaseStatus.RUNNING.value, LeaseStatus.UPLOADING.value):
        return rejected("LEASE_NOT_RUNNING", lease_status=lease.status)
    indexes = sorted(f.index for f in payload.files)
    if indexes != list(range(gen.number_of_images)):
        return rejected("FILE_COUNT_MISMATCH", expected=gen.number_of_images)
    for f in payload.files:
        if f.mime_type not in s.allowed_image_mime_types:
            return rejected("UNSUPPORTED_MEDIA_TYPE", index=f.index)
        if f.size > s.max_image_bytes:
            return rejected("FILE_TOO_LARGE", index=f.index, max_bytes=s.max_image_bytes)

    existing = {k["index"]: k for k in lease.upload_keys}
    keys: list[dict[str, Any]] = []
    uploads: list[dict[str, Any]] = []
    for f in sorted(payload.files, key=lambda x: x.index):
        prev = existing.get(f.index)
        ext = images.EXTENSIONS[f.mime_type]
        if prev and prev["mime_type"] == f.mime_type:
            key = prev["storage_key"]
        else:
            key = f"generations/{gen.user_id}/{gen.id}/{f.index}-{secrets.token_hex(8)}.{ext}"
        keys.append({"index": f.index, "storage_key": key, "mime_type": f.mime_type, "sha256": f.sha256.lower(),
                     "size": f.size})
        uploads.append({"index": f.index, "storage_key": key, "method": "PUT",
                        "url": s3.presign_put(key, f.mime_type),
                        "headers": {"Content-Type": f.mime_type}})
    lease.upload_keys = keys
    first_upload = lease.status != LeaseStatus.UPLOADING.value
    lease.status = LeaseStatus.UPLOADING.value
    gen.status = GenerationStatus.UPLOADING.value
    after = [_notify(gen, "generation.progress")] if first_upload else []
    return Outcome(result={"uploads": uploads, "expires_in": s.upload_url_ttl_seconds}, after_commit=after)


@dataclass
class VerifiedFile:
    index: int
    storage_key: str
    thumbnail_key: str
    info: images.ImageInfo


async def verify_uploaded_files(issued: list[dict[str, Any]], files: list[p.UploadedFile]
                                ) -> tuple[list[VerifiedFile], str | None]:
    """Downloads each object from the private bucket and validates it.
    Pure I/O; runs *outside* the DB transaction. Returns (files, error_code)."""
    s = get_settings()
    by_index = {k["index"]: k for k in issued}
    verified: list[VerifiedFile] = []
    for f in files:
        spec = by_index.get(f.index)
        if spec is None or spec["storage_key"] != f.storage_key:
            return [], "UNKNOWN_STORAGE_KEY"
        if f.sha256.lower() != spec["sha256"]:
            return [], "CHECKSUM_MISMATCH"
        try:
            data = await s3.get_bytes(f.storage_key, s.max_image_bytes)
        except s3.ObjectMissing:
            return [], "OBJECT_MISSING"
        except s3.ObjectTooLarge:
            return [], "FILE_TOO_LARGE"
        try:
            info = await asyncio.to_thread(
                images.validate_image, data, allowed_mime=s.allowed_image_mime_types, max_bytes=s.max_image_bytes,
                max_pixels=s.max_image_pixels, expected_sha256=spec["sha256"], declared_mime=spec["mime_type"])
        except images.ImageValidationError as exc:
            log.warning("uploaded image rejected", extra={"extra_fields": {"key": f.storage_key, "code": exc.code}})
            return [], exc.code
        thumb = await asyncio.to_thread(images.make_thumbnail, data, s.thumbnail_size)
        thumb_key = f.storage_key.rsplit(".", 1)[0] + ".thumb.webp"
        await s3.put_bytes(thumb_key, thumb, "image/webp")
        verified.append(VerifiedFile(f.index, f.storage_key, thumb_key, info))
    if sorted(v.index for v in verified) != sorted(k["index"] for k in issued):
        return [], "FILE_COUNT_MISMATCH"
    return verified, None


async def handle_upload_completed(db: AsyncSession, worker_id: uuid.UUID, payload: p.UploadCompletedPayload,
                                  verified: list[VerifiedFile], verify_error: str | None) -> Outcome:
    loaded = await _load_task(db, worker_id, payload.task_id)
    if loaded is None:
        return rejected("UNKNOWN_TASK")
    gen, lease = loaded
    if lease.status != LeaseStatus.UPLOADING.value:
        return rejected("LEASE_NOT_UPLOADING", lease_status=lease.status)
    if verify_error:
        lease.status = LeaseStatus.FAILED.value
        lease.finished_at = _now()
        wallet = await generation_service.finalize_failure(
            db, gen, error_code="ASSET_VERIFICATION_FAILED", error_message=verify_error,
            status=GenerationStatus.FAILED)

        async def _emit() -> None:
            await generation_service.emit_terminal_events(gen, wallet)
        return Outcome(status="rejected", result={"reason": "ASSET_VERIFICATION_FAILED", "detail": verify_error},
                       after_commit=[_emit])
    existing = {a.image_index: a for a in (await db.execute(
        select(Asset).where(Asset.generation_id == gen.id))).scalars()}
    for v in verified:
        if v.index in existing:
            continue  # replay
        db.add(Asset(generation_id=gen.id, user_id=gen.user_id, image_index=v.index, storage_key=v.storage_key,
                     thumbnail_key=v.thumbnail_key, mime_type=v.info.mime_type, width=v.info.width,
                     height=v.info.height, file_size=v.info.file_size, sha256=v.info.sha256,
                     metadata_json={"verified_at": _now().isoformat()}))
    await db.flush()
    return Outcome(result={"verified": [v.index for v in verified]})


async def handle_task_completed(db: AsyncSession, worker_id: uuid.UUID, payload: p.TaskCompletedPayload) -> Outcome:
    loaded = await _load_task(db, worker_id, payload.task_id)
    if loaded is None:
        return rejected("UNKNOWN_TASK")
    gen, lease = loaded
    if gen.status == GenerationStatus.COMPLETED.value:
        return Outcome(result={"generation_status": "completed"})
    if lease.status != LeaseStatus.UPLOADING.value:
        return rejected("LEASE_NOT_UPLOADING", lease_status=lease.status)
    asset_count = (await db.execute(
        select(func.count()).select_from(Asset).where(Asset.generation_id == gen.id))).scalar_one()
    if asset_count != gen.number_of_images:
        # Never mark COMPLETED before every image is stored and verified.
        return rejected("ASSETS_NOT_VERIFIED", stored=asset_count, expected=gen.number_of_images)

    db.add(GenerationMetadata(generation_id=gen.id, provider=payload.provider or gen.provider, model=payload.model,
                              provider_metadata=payload.provider_metadata))
    lease.status = LeaseStatus.COMPLETED.value
    lease.finished_at = _now()
    gen.status = GenerationStatus.COMPLETED.value
    gen.progress = 100
    gen.completed_at = _now()
    gen.error_code = None
    gen.error_message = None
    wallet = await credit_service.settle(db, gen)
    asset_ids = [str(a) for a in (await db.execute(
        select(Asset.id).where(Asset.generation_id == gen.id).order_by(Asset.image_index))).scalars()]
    wallet_view = credit_service.wallet_view(wallet) if wallet else None

    async def _emit() -> None:
        await generation_service.emit_terminal_events(gen, wallet_view, {"asset_ids": asset_ids})
    return Outcome(result={"generation_status": "completed"}, after_commit=[_emit])


async def handle_task_failed(db: AsyncSession, worker_id: uuid.UUID, payload: p.TaskFailedPayload) -> Outcome:
    loaded = await _load_task(db, worker_id, payload.task_id)
    if loaded is None:
        return rejected("UNKNOWN_TASK")
    gen, lease = loaded
    if lease.status not in (LeaseStatus.ACCEPTED.value, LeaseStatus.RUNNING.value, LeaseStatus.UPLOADING.value):
        return rejected("LEASE_NOT_ACTIVE", lease_status=lease.status)
    lease.status = LeaseStatus.FAILED.value
    lease.finished_at = _now()
    code = payload.error_code.upper()

    # Mark the account/session state. We do NOT move the job to another
    # account: limits and re-authentication are surfaced, not evaded.
    if lease.account_session_id:
        sess = await db.get(AccountSession, lease.account_session_id)
        if sess is not None and code == "AUTH_REQUIRED":
            sess.status = SessionStatus.AUTH_REQUIRED.value
            await db.flush()
            await refresh_account_session_status(db, sess.account_id)
        if sess is not None and code == "PROVIDER_LIMITED":
            acct = await db.get(GoogleAccount, sess.account_id)
            if acct is not None:
                acct.status = AccountStatus.LIMITED.value
                acct.limited_until = _now() + timedelta(seconds=payload.retry_after_seconds or 900)

    wallet = await generation_service.finalize_failure(db, gen, error_code=code,
                                                       error_message=payload.error_message or code)

    async def _emit() -> None:
        await generation_service.emit_terminal_events(gen, wallet)
    return Outcome(result={"generation_status": gen.status.lower()}, after_commit=[_emit])
