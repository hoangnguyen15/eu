from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import CurrentUser, client_ip, get_current_user, rate_limit_ip
from app.schemas import Envelope, LoginRequest, RefreshRequest, RegisterRequest, TokenPair, UserProfile, ok
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])
_auth_limit = rate_limit_ip("auth", lambda: get_settings().rate_limit_auth_per_minute)


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=Envelope[UserProfile],
             dependencies=[Depends(_auth_limit)])
async def register(body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    user = await auth_service.register(db, body.email, body.password, client_ip(request))
    return ok({"id": user.id, "email": user.email, "role": user.role, "status": user.status,
               "created_at": user.created_at})


@router.post("/login", response_model=Envelope[TokenPair], dependencies=[Depends(_auth_limit)])
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    return ok(await auth_service.login(db, body.email, body.password, client_ip(request)))


@router.post("/refresh", response_model=Envelope[TokenPair], dependencies=[Depends(_auth_limit)])
async def refresh(body: RefreshRequest, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    return ok(await auth_service.refresh(db, body.refresh_token, client_ip(request)))


@router.post("/logout", response_model=Envelope[dict])
async def logout(body: RefreshRequest, request: Request, user: CurrentUser = Depends(get_current_user),
                 db: AsyncSession = Depends(get_db)) -> dict:
    await auth_service.logout(db, body.refresh_token, user.id, client_ip(request))
    return ok({"logged_out": True})
