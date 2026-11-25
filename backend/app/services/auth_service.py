"""Registration, login and refresh-token rotation with reuse detection."""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import (
    burn_password_check,
    create_access_token,
    hash_password,
    hash_token,
    new_opaque_token,
    password_needs_rehash,
    utcnow,
    verify_password,
)
from app.models import RefreshToken, User, UserStatus
from app.services import credit_service
from app.services.audit import audit


async def register(db: AsyncSession, email: str, password: str, ip: str | None) -> User:
    user = User(email=email.lower(), password_hash=hash_password(password))
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("Email already registered", code="EMAIL_TAKEN") from exc
    bonus = get_settings().signup_bonus_credits
    await credit_service.get_or_create_wallet(db, user.id)
    if bonus > 0:
        await credit_service.grant(db, user.id, bonus, note="signup_bonus")
    audit(db, action="user.register", resource_type="user", resource_id=user.id, actor_id=user.id, ip=ip)
    await db.commit()
    return user


async def _issue_tokens(db: AsyncSession, user: User, family_id: uuid.UUID | None = None) -> tuple[dict, RefreshToken]:
    s = get_settings()
    access, ttl = create_access_token(user.id, user.role)
    raw_refresh = new_opaque_token()
    rt = RefreshToken(
        user_id=user.id,
        token_hash=hash_token(raw_refresh),
        family_id=family_id or uuid.uuid4(),
        expires_at=utcnow() + timedelta(seconds=s.refresh_token_ttl_seconds),
    )
    db.add(rt)
    await db.flush()
    return {"access_token": access, "refresh_token": raw_refresh, "token_type": "bearer", "expires_in": ttl}, rt


async def login(db: AsyncSession, email: str, password: str, ip: str | None) -> dict:
    user = (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
    if user is None:
        burn_password_check(password)
        raise UnauthorizedError("Invalid email or password", code="INVALID_CREDENTIALS")
    if not verify_password(user.password_hash, password):
        audit(db, action="user.login_failed", resource_type="user", resource_id=user.id, ip=ip)
        await db.commit()
        raise UnauthorizedError("Invalid email or password", code="INVALID_CREDENTIALS")
    if user.status != UserStatus.ACTIVE.value:
        raise UnauthorizedError("Account disabled", code="ACCOUNT_DISABLED")
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    tokens, _ = await _issue_tokens(db, user)
    audit(db, action="user.login", resource_type="user", resource_id=user.id, actor_id=user.id, ip=ip)
    await db.commit()
    return tokens


async def _revoke_family(db: AsyncSession, family_id: uuid.UUID) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


async def refresh(db: AsyncSession, raw_token: str, ip: str | None) -> dict:
    stmt = select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token)).with_for_update()
    rt = (await db.execute(stmt)).scalar_one_or_none()
    if rt is None:
        raise UnauthorizedError("Invalid refresh token", code="INVALID_REFRESH_TOKEN")
    if rt.revoked_at is not None:
        # Reuse of a rotated token => likely theft. Kill the whole family.
        await _revoke_family(db, rt.family_id)
        audit(db, action="auth.refresh_reuse_detected", resource_type="user", resource_id=rt.user_id, ip=ip,
              actor_id=rt.user_id)
        await db.commit()
        raise UnauthorizedError("Refresh token reuse detected", code="REFRESH_TOKEN_REUSED")
    if rt.expires_at <= utcnow():
        raise UnauthorizedError("Refresh token expired", code="REFRESH_TOKEN_EXPIRED")
    user = await db.get(User, rt.user_id)
    if user is None or user.status != UserStatus.ACTIVE.value:
        raise UnauthorizedError("Account disabled", code="ACCOUNT_DISABLED")
    tokens, new_rt = await _issue_tokens(db, user, family_id=rt.family_id)
    rt.revoked_at = utcnow()
    rt.replaced_by_id = new_rt.id
    await db.commit()
    return tokens


async def logout(db: AsyncSession, raw_token: str, user_id: uuid.UUID, ip: str | None) -> None:
    stmt = select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token))
    rt = (await db.execute(stmt)).scalar_one_or_none()
    if rt is not None and rt.user_id == user_id:
        await _revoke_family(db, rt.family_id)
        audit(db, action="user.logout", resource_type="user", resource_id=user_id, actor_id=user_id, ip=ip)
        await db.commit()
