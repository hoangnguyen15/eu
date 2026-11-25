"""Scheduler: assigns QUEUED generations to worker slots and recovers from
expired leases / offline workers. Safe to run as several replicas:
generation rows are claimed with FOR UPDATE SKIP LOCKED and the partial
unique indexes on task_leases make double-dispatch impossible."""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.redis import agent_channel, get_redis
from app.models import (
    ACTIVE_LEASE_STATUSES,
    AccountSession,
    AccountStatus,
    AuthorizationStatus,
    Generation,
    GenerationStatus,
    GoogleAccount,
    LeaseStatus,
    SessionStatus,
    TaskLease,
    Workstation,
    WorkstationStatus,
)
from app.scheduler.offers import build_offer
from app.services import generation_service
from app.services.audit import audit
from app.workers.handlers import requeue_or_dead_letter

log = logging.getLogger(__name__)
_ACTIVE = [s.value for s in ACTIVE_LEASE_STATUSES]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Candidate:
    session_id: uuid.UUID
    account_id: uuid.UUID
    worker_id: uuid.UUID
    capacity: int
    active: int


async def find_candidates(db: AsyncSession, provider: str) -> list[Candidate]:
    s = get_settings()
    active_per_worker = (
        select(TaskLease.worker_id, func.count().label("n"))
        .where(TaskLease.status.in_(_ACTIVE)).group_by(TaskLease.worker_id).subquery()
    )
    active_per_account = (
        select(AccountSession.account_id, func.count().label("n"))
        .join(TaskLease, TaskLease.account_session_id == AccountSession.id)
        .where(TaskLease.status.in_(_ACTIVE)).group_by(AccountSession.account_id).subquery()
    )
    w_active = func.coalesce(active_per_worker.c.n, 0)
    a_active = func.coalesce(active_per_account.c.n, 0)
    stmt = (
        select(AccountSession.id, AccountSession.account_id, Workstation.id, Workstation.capacity, w_active)
        .join(Workstation, Workstation.id == AccountSession.workstation_id)
        .join(GoogleAccount, GoogleAccount.id == AccountSession.account_id)
        .outerjoin(active_per_worker, active_per_worker.c.worker_id == Workstation.id)
        .outerjoin(active_per_account, active_per_account.c.account_id == GoogleAccount.id)
        .where(
            AccountSession.provider == provider,
            AccountSession.status == SessionStatus.ACTIVE.value,
            Workstation.status == WorkstationStatus.ONLINE.value,
            Workstation.last_heartbeat_at > _now() - timedelta(seconds=s.worker_offline_after_seconds),
            Workstation.providers.contains([provider]),
            GoogleAccount.authorization_status == AuthorizationStatus.AUTHORIZED.value,
            GoogleAccount.status == AccountStatus.ACTIVE.value,
            GoogleAccount.session_status == SessionStatus.ACTIVE.value,
            w_active < Workstation.capacity,
            a_active < s.max_concurrent_jobs_per_account,
        )
        # least-loaded worker first; random tie-break spreads load across a fleet
        .order_by((w_active * 1.0 / func.greatest(Workstation.capacity, 1)).asc(), func.random())
    )
    return [Candidate(r[0], r[1], r[2], r[3], r[4]) for r in (await db.execute(stmt)).all()]


async def _free_slot(db: AsyncSession, worker_id: uuid.UUID, capacity: int) -> int | None:
    used = set((await db.execute(
        select(TaskLease.slot_index).where(TaskLease.worker_id == worker_id, TaskLease.status.in_(_ACTIVE))
    )).scalars().all())
    for i in range(capacity):
        if i not in used:
            return i
    return None


async def dispatch_one(db: AsyncSession, generation_id: uuid.UUID) -> tuple[bool, dict | None, uuid.UUID | None]:
    """Try to dispatch one generation. Returns (no_capacity, offer, worker_id)."""
    s = get_settings()
    gen = (await db.execute(
        select(Generation).where(Generation.id == generation_id, Generation.status == GenerationStatus.QUEUED.value)
        .with_for_update(skip_locked=True)
    )).scalar_one_or_none()
    if gen is None:
        return False, None, None
    for cand in await find_candidates(db, gen.provider):
        slot = await _free_slot(db, cand.worker_id, cand.capacity)
        if slot is None:
            continue
        lease = TaskLease(id=uuid.uuid4(), generation_id=gen.id, worker_id=cand.worker_id,
                          account_session_id=cand.session_id, slot_index=slot,
                          attempt=gen.dispatch_attempts + 1, status=LeaseStatus.OFFERED.value,
                          expires_at=_now() + timedelta(seconds=s.offer_ttl_seconds))
        try:
            async with db.begin_nested():
                db.add(lease)
                await db.flush()
        except IntegrityError:
            continue  # slot taken concurrently by another scheduler replica
        gen.status = GenerationStatus.DISPATCHED.value
        gen.worker_id = cand.worker_id
        gen.account_id = cand.account_id
        gen.dispatch_attempts += 1
        gen.dispatched_at = _now()
        offer = await build_offer(db, lease, gen)
        return False, offer, cand.worker_id
    return True, None, None


async def dispatch_pending(db_factory: Callable[[], AsyncSession]) -> int:
    s = get_settings()
    async with db_factory() as db:
        rows = (await db.execute(
            select(Generation.id, Generation.provider).where(Generation.status == GenerationStatus.QUEUED.value)
            .order_by(Generation.created_at).limit(s.scheduler_batch_size)
        )).all()
    saturated: set[str] = set()
    dispatched = 0
    for gen_id, provider in rows:
        if provider in saturated:
            continue
        async with db_factory() as db:
            no_capacity, offer, worker_id = await dispatch_one(db, gen_id)
            await db.commit()
        if no_capacity:
            saturated.add(provider)
            continue
        if offer is not None and worker_id is not None:
            dispatched += 1
            await _publish_offer(worker_id, offer)
    return dispatched


async def _publish_offer(worker_id: uuid.UUID, offer: dict) -> None:
    import json
    try:
        await get_redis().publish(agent_channel(worker_id), json.dumps(offer, default=str))
    except Exception:  # lease expiry will requeue the job
        log.warning("failed to publish offer", exc_info=True)


# --------------------------------------------------------------- recovery ----
async def mark_offline_workers(db: AsyncSession) -> list[uuid.UUID]:
    s = get_settings()
    rows = (await db.execute(
        update(Workstation)
        .where(Workstation.status == WorkstationStatus.ONLINE.value,
               or_(Workstation.last_heartbeat_at.is_(None),
                   Workstation.last_heartbeat_at < _now() - timedelta(seconds=s.worker_offline_after_seconds)))
        .values(status=WorkstationStatus.OFFLINE.value).returning(Workstation.id)
    )).scalars().all()
    for wid in rows:
        audit(db, action="workstation.offline", resource_type="workstation", resource_id=wid, actor_type="system")
    return list(rows)


async def recover_leases(db_factory: Callable[[], AsyncSession]) -> dict[str, int]:
    """* OFFERED leases that expired, or whose worker went offline -> requeue
         (provider never invoked, so this is safe).
       * ACCEPTED/RUNNING/UPLOADING leases past their deadline -> FAILED and
         refunded. Not retried: the provider-side outcome is unknown."""
    stats = {"requeued": 0, "failed": 0}
    async with db_factory() as db:
        offline = select(Workstation.id).where(Workstation.status != WorkstationStatus.ONLINE.value)
        expired = (await db.execute(
            select(TaskLease.id, TaskLease.generation_id, TaskLease.status, TaskLease.worker_id).where(
                TaskLease.status.in_(_ACTIVE),
                or_(TaskLease.expires_at < _now(),
                    and_(TaskLease.status == LeaseStatus.OFFERED.value, TaskLease.worker_id.in_(offline))),
            ).limit(500)
        )).all()

    for lease_id, gen_id, _status, worker_id in expired:
        after: list[Callable[[], Awaitable[None]]] = []
        async with db_factory() as db:
            gen = (await db.execute(select(Generation).where(Generation.id == gen_id)
                                    .with_for_update(skip_locked=True))).scalar_one_or_none()
            if gen is None:
                continue
            lease = (await db.execute(select(TaskLease).where(TaskLease.id == lease_id).with_for_update())
                     ).scalar_one()
            if lease.status not in _ACTIVE:
                await db.commit()
                continue
            was_offer = lease.status == LeaseStatus.OFFERED.value
            lease.status = LeaseStatus.EXPIRED.value
            lease.finished_at = _now()
            if was_offer:
                after = await requeue_or_dead_letter(db, gen, reason="offer expired")
                stats["requeued"] += 1
            else:
                ws = await db.get(Workstation, worker_id)
                code = "WORKER_LOST" if ws is None or ws.status != WorkstationStatus.ONLINE.value else "JOB_TIMEOUT"
                wallet = await generation_service.finalize_failure(
                    db, gen, error_code=code, error_message="Worker did not report a result before the lease expired",
                    status=GenerationStatus.FAILED)
                audit(db, action="generation.lease_expired", resource_type="generation", resource_id=gen.id,
                      actor_type="system", details={"lease_id": str(lease_id), "code": code})
                stats["failed"] += 1

                async def _emit(gen: Generation = gen, wallet: dict | None = wallet, wid: uuid.UUID = worker_id,
                                lid: uuid.UUID = lease_id) -> None:
                    await generation_service.emit_terminal_events(gen, wallet)
                    await generation_service.notify_agent(wid, "TASK_CANCEL",
                                                          {"task_id": str(lid), "reason": "LEASE_EXPIRED"})
                after = [_emit]
            await db.commit()
        for fn in after:
            try:
                await fn()
            except Exception:
                log.exception("recovery side effect failed")
    return stats


async def expire_stale_queue(db_factory: Callable[[], AsyncSession]) -> int:
    s = get_settings()
    async with db_factory() as db:
        ids = (await db.execute(
            select(Generation.id).where(Generation.status == GenerationStatus.QUEUED.value,
                                        Generation.created_at < _now() - timedelta(seconds=s.queue_timeout_seconds))
            .limit(200)
        )).scalars().all()
    n = 0
    for gid in ids:
        async with db_factory() as db:
            gen = (await db.execute(select(Generation).where(Generation.id == gid,
                                                             Generation.status == GenerationStatus.QUEUED.value)
                                    .with_for_update(skip_locked=True))).scalar_one_or_none()
            if gen is None:
                continue
            wallet = await generation_service.finalize_failure(
                db, gen, error_code="NO_WORKER_AVAILABLE", error_message="No eligible worker within queue timeout",
                status=GenerationStatus.FAILED)
            await db.commit()
        await generation_service.emit_terminal_events(gen, wallet)
        n += 1
    return n


async def restore_limited_accounts(db: AsyncSession) -> int:
    res = await db.execute(
        update(GoogleAccount)
        .where(GoogleAccount.status == AccountStatus.LIMITED.value, GoogleAccount.limited_until < _now())
        .values(status=AccountStatus.ACTIVE.value, limited_until=None)
    )
    return res.rowcount or 0


async def tick(db_factory: Callable[[], AsyncSession]) -> dict[str, int]:
    async with db_factory() as db:
        # Serialize the cheap global maintenance step across scheduler replicas.
        got = (await db.execute(text("SELECT pg_try_advisory_xact_lock(727001)"))).scalar_one()
        offline: list[uuid.UUID] = []
        restored = 0
        if got:
            offline = await mark_offline_workers(db)
            restored = await restore_limited_accounts(db)
        await db.commit()
    rec = await recover_leases(db_factory)
    stale = await expire_stale_queue(db_factory)
    dispatched = await dispatch_pending(db_factory)
    return {"offline": len(offline), "restored": restored, "dispatched": dispatched, "stale": stale, **rec}
