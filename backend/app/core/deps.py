"""FastAPI dependencies: DB session, current user, RBAC and rate limiting."""
from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.exceptions import ForbiddenError, RateLimitedError, UnauthorizedError
from app.core.redis import get_redis
from app.core.security import decode_access_token
from app.models import User, UserRole, UserStatus

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    role: str
    email: str

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN.value


def client_ip(request: Request) -> str:
    """Client IP, honouring X-Forwarded-For only for the configured number of trusted proxies."""
    hops = get_settings().trusted_proxy_count
    if hops > 0:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if len(parts) >= hops:
                return parts[-hops]
    return request.client.host if request.client else "unknown"


async def authenticate_token(token: str, db: AsyncSession) -> CurrentUser:
    payload = decode_access_token(token)
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("Invalid access token", code="INVALID_TOKEN") from exc
    user = await db.get(User, user_id)
    if user is None or user.status != UserStatus.ACTIVE.value:
        raise UnauthorizedError("User not found or disabled", code="INVALID_TOKEN")
    return CurrentUser(id=user.id, role=user.role, email=user.email)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    if creds is None or creds.scheme.lower() != "bearer":
        raise UnauthorizedError("Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    return await authenticate_token(creds.credentials, db)


async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise ForbiddenError("Admin role required")
    return user


async def hit_rate_limit(scope: str, ident: str, limit: int, window_seconds: int = 60) -> None:
    """Fixed-window counter in Redis. Fails open if Redis is unavailable so an
    outage of the limiter does not take the API down (logged by caller)."""
    if not get_settings().rate_limit_enabled or limit <= 0:
        return
    window = int(time.time() // window_seconds)
    key = f"rl:{scope}:{ident}:{window}"
    redis = get_redis()
    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_seconds + 1)
    count, _ = await pipe.execute()
    if int(count) > limit:
        retry_after = window_seconds - int(time.time() % window_seconds)
        raise RateLimitedError("Too many requests", headers={"Retry-After": str(retry_after)},
                               details={"limit": limit, "window_seconds": window_seconds})


def rate_limit_ip(scope: str, limit_getter: Callable[[], int]) -> Callable[[Request], Awaitable[None]]:
    async def _dep(request: Request) -> None:
        await hit_rate_limit(scope, client_ip(request), limit_getter())
    return _dep


def rate_limit_user(scope: str, limit_getter: Callable[[], int]) -> Callable[..., Awaitable[None]]:
    async def _dep(user: CurrentUser = Depends(get_current_user)) -> None:
        await hit_rate_limit(scope, str(user.id), limit_getter())
    return _dep
