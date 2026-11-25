"""Admin (RBAC role=admin) endpoints: enrollment, workstations, provider
accounts, credits, review queue, dead letters and audit log."""
from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import CurrentUser, client_ip, require_admin
from app.core.exceptions import ConflictError, NotFoundError
from app.core.security import hash_token, new_opaque_token, utcnow
from app.models import (
    ACTIVE_LEASE_STATUSES,
    AuditLog,
    EnrollmentToken,
    Generation,
    GenerationStatus,
    GoogleAccount,
    TaskLease,
    User,
    Workstation,
    WorkstationStatus,
)
from app.schemas import (
    CreditGrant,
    EnrollmentTokenCreate,
    EnrollmentTokenOut,
    Envelope,
    GoogleAccountCreate,
    GoogleAccountOut,
    GoogleAccountUpdate,
    ResolveReview,
    WorkstationOut,
    ok,
)
from app.services import credit_service, generation_service
from app.services.audit import audit
from app.services.events import publish_user_event

router = APIRouter(prefix="/admin", tags=["admin"])


def _acct(a: GoogleAccount) -> dict:
    return {c: getattr(a, c) for c in GoogleAccountOut.model_fields}


@router.post("/enrollment-tokens", response_model=Envelope[EnrollmentTokenOut], status_code=201)
async def create_enrollment_token(body: EnrollmentTokenCreate, request: Request,
                                  admin: CurrentUser = Depends(require_admin),
                                  db: AsyncSession = Depends(get_db)) -> dict:
    raw = new_opaque_token(32)
    tok = EnrollmentToken(token_hash=hash_token(raw), workstation_name=body.workstation_name, created_by=admin.id,
                          expires_at=utcnow() + timedelta(seconds=get_settings().enrollment_token_ttl_seconds))
    db.add(tok)
    await db.flush()
    audit(db, action="enrollment_token.create", resource_type="enrollment_token", resource_id=tok.id,
          actor_id=admin.id, ip=client_ip(request), details={"workstation_name": body.workstation_name})
    await db.commit()
    return ok({"id": tok.id, "token": raw, "workstation_name": tok.workstation_name, "expires_at": tok.expires_at})


@router.get("/workstations", response_model=Envelope[list[WorkstationOut]])
async def list_workstations(_: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    active = (select(TaskLease.worker_id, func.count().label("n"))
              .where(TaskLease.status.in_([s.value for s in ACTIVE_LEASE_STATUSES]))
              .group_by(TaskLease.worker_id).subquery())
    rows = (await db.execute(select(Workstation, func.coalesce(active.c.n, 0))
                             .outerjoin(active, active.c.worker_id == Workstation.id)
                             .order_by(Workstation.name))).all()
    out = []
    for w, n in rows:
        d = {c: getattr(w, c) for c in WorkstationOut.model_fields if c != "active_leases"}
        d["active_leases"] = n
        out.append(d)
    return ok(out)


@router.post("/workstations/{workstation_id}/disable", response_model=Envelope[dict])
async def disable_workstation(workstation_id: uuid.UUID, request: Request,
                              admin: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    ws = await db.get(Workstation, workstation_id)
    if ws is None:
        raise NotFoundError("Workstation not found")
    ws.status = WorkstationStatus.DISABLED.value
    audit(db, action="workstation.disable", resource_type="workstation", resource_id=ws.id, actor_id=admin.id,
          ip=client_ip(request))
    await db.commit()
    await generation_service.notify_agent(ws.id, "_SUPERSEDED", {"conn_id": "disabled"})
    return ok({"id": ws.id, "status": ws.status})


@router.post("/google-accounts", response_model=Envelope[GoogleAccountOut], status_code=201)
async def create_account(body: GoogleAccountCreate, request: Request, admin: CurrentUser = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)) -> dict:
    acct = GoogleAccount(google_email=body.google_email.lower(), owner_id=admin.id,
                         authorization_status=body.authorization_status)
    db.add(acct)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ConflictError("Account already registered") from exc
    audit(db, action="google_account.create", resource_type="google_account", resource_id=acct.id,
          actor_id=admin.id, ip=client_ip(request),
          details={"authorization_status": body.authorization_status, "attestation": body.attestation})
    await db.commit()
    await db.refresh(acct)
    return ok(_acct(acct))


@router.get("/google-accounts", response_model=Envelope[list[GoogleAccountOut]])
async def list_accounts(_: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(GoogleAccount).order_by(GoogleAccount.created_at))).scalars().all()
    return ok([_acct(a) for a in rows])


@router.patch("/google-accounts/{account_id}", response_model=Envelope[GoogleAccountOut])
async def update_account(account_id: uuid.UUID, body: GoogleAccountUpdate, request: Request,
                         admin: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    acct = await db.get(GoogleAccount, account_id)
    if acct is None:
        raise NotFoundError("Account not found")
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(acct, k, v)
    if changes.get("status") == "ACTIVE":
        acct.limited_until = None
    audit(db, action="google_account.update", resource_type="google_account", resource_id=acct.id,
          actor_id=admin.id, ip=client_ip(request), details=changes)
    await db.commit()
    await db.refresh(acct)
    return ok(_acct(acct))


@router.post("/credits/grant", response_model=Envelope[dict])
async def grant_credits(body: CreditGrant, request: Request, admin: CurrentUser = Depends(require_admin),
                        db: AsyncSession = Depends(get_db)) -> dict:
    if await db.get(User, body.user_id) is None:
        raise NotFoundError("User not found")
    wallet = await credit_service.grant(db, body.user_id, body.amount, note=body.note or f"admin:{admin.id}")
    audit(db, action="credits.grant", resource_type="user", resource_id=body.user_id, actor_id=admin.id,
          ip=client_ip(request), details={"amount": body.amount})
    await db.commit()
    view = credit_service.wallet_view(wallet)
    await publish_user_event(body.user_id, "credits.updated", view)
    return ok(view)


@router.get("/generations", response_model=Envelope[list[dict]])
async def admin_generations(status: str = Query("REVIEW_REQUIRED", max_length=24),
                            limit: int = Query(50, ge=1, le=200),
                            _: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(Generation).where(Generation.status == status.upper())
                             .order_by(Generation.created_at.desc()).limit(limit))).scalars().all()
    return ok([{"id": g.id, "user_id": g.user_id, "status": g.status, "error_code": g.error_code,
                "worker_id": g.worker_id, "account_id": g.account_id, "credit_state": g.credit_state,
                "dispatch_attempts": g.dispatch_attempts, "created_at": g.created_at} for g in rows])


@router.get("/dead-letters", response_model=Envelope[list[dict]])
async def dead_letters(limit: int = Query(50, ge=1, le=200), _: CurrentUser = Depends(require_admin),
                       db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(select(Generation).where(Generation.error_code == "DEAD_LETTERED")
                             .order_by(Generation.created_at.desc()).limit(limit))).scalars().all()
    return ok([{"id": g.id, "user_id": g.user_id, "dispatch_attempts": g.dispatch_attempts,
                "error_message": g.error_message, "created_at": g.created_at} for g in rows])


@router.post("/generations/{generation_id}/resolve", response_model=Envelope[dict])
async def resolve_review(generation_id: uuid.UUID, body: ResolveReview, request: Request,
                         admin: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    """For REVIEW_REQUIRED generations whose credits are still reserved."""
    gen = await generation_service.lock_generation(db, generation_id)
    if gen is None:
        raise NotFoundError("Generation not found")
    if gen.status != GenerationStatus.REVIEW_REQUIRED.value:
        raise ConflictError("Generation is not awaiting review", code="INVALID_STATE")
    if body.action == "refund":
        wallet = await credit_service.release(db, gen, note=body.note or "review:refund")
    else:
        wallet = await credit_service.settle(db, gen)
    audit(db, action=f"generation.review_{body.action}", resource_type="generation", resource_id=gen.id,
          actor_id=admin.id, ip=client_ip(request), details={"note": body.note})
    await db.commit()
    if wallet is not None:
        await publish_user_event(gen.user_id, "credits.updated", credit_service.wallet_view(wallet))
    return ok({"id": gen.id, "credit_state": gen.credit_state})


@router.get("/audit-logs", response_model=Envelope[list[dict]])
async def audit_logs(limit: int = Query(100, ge=1, le=500), resource_type: str | None = Query(None, max_length=64),
                     _: CurrentUser = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    rows = (await db.execute(stmt)).scalars().all()
    return ok([{"id": r.id, "actor_id": r.actor_id, "actor_type": r.actor_type, "action": r.action,
                "resource_type": r.resource_type, "resource_id": r.resource_id, "ip_address": r.ip_address,
                "details": r.details, "created_at": r.created_at} for r in rows])
