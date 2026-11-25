"""Agent enrollment: exchanges a single-use enrollment token + CSR for a client
certificate used for mTLS on the agent gateway."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import client_ip, rate_limit_ip
from app.core.exceptions import AppError, UnauthorizedError
from app.core.security import hash_token, utcnow
from app.models import EnrollmentToken, Workstation, WorkstationStatus
from app.schemas import AgentEnrollRequest, AgentEnrollResponse, Envelope, ok
from app.services.audit import audit
from app.workers import pki

router = APIRouter(prefix="/agent", tags=["agent"])
_limit = rate_limit_ip("enroll", lambda: get_settings().rate_limit_auth_per_minute)


@router.post("/enroll", response_model=Envelope[AgentEnrollResponse], dependencies=[Depends(_limit)])
async def enroll(body: AgentEnrollRequest, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    ip = client_ip(request)
    tok = (await db.execute(
        select(EnrollmentToken).where(EnrollmentToken.token_hash == hash_token(body.token)).with_for_update()
    )).scalar_one_or_none()
    if tok is None or tok.used_at is not None or tok.expires_at <= utcnow():
        audit(db, action="workstation.enroll_denied", resource_type="enrollment_token",
              resource_id=tok.id if tok else None, actor_type="worker", ip=ip)
        await db.commit()
        raise UnauthorizedError("Enrollment token invalid, expired or already used", code="INVALID_ENROLLMENT_TOKEN")

    ws = Workstation(name=tok.workstation_name, hostname=body.hostname, status=WorkstationStatus.OFFLINE.value)
    db.add(ws)
    await db.flush()
    try:
        cert = pki.sign_agent_csr(body.csr_pem, ws.id)
    except pki.PkiError as exc:
        raise AppError(str(exc), code="INVALID_CSR", status_code=422) from exc
    ws.cert_fingerprint = pki.fingerprint(cert)
    ws.cert_serial = format(cert.serial_number, "x")
    ws.cert_expires_at = cert.not_valid_after_utc
    tok.used_at = utcnow()
    tok.workstation_id = ws.id
    audit(db, action="workstation.enrolled", resource_type="workstation", resource_id=ws.id, actor_type="worker",
          ip=ip, details={"name": ws.name, "hostname": body.hostname, "serial": ws.cert_serial})
    await db.commit()
    return ok({"workstation_id": ws.id, "certificate_pem": pki.cert_pem(cert),
               "ca_certificate_pem": pki.ca_cert_pem(), "expires_at": ws.cert_expires_at})
