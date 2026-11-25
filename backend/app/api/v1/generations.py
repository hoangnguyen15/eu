from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import CurrentUser, client_ip, get_current_user, rate_limit_user
from app.schemas import Envelope, GenerationCreate, GenerationCreated, GenerationOut, GenerationPage, ok
from app.services import generation_service

router = APIRouter(prefix="/generations", tags=["generations"])
_gen_limit = rate_limit_user("generations", lambda: get_settings().rate_limit_generations_per_minute)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=Envelope[GenerationCreated],
             dependencies=[Depends(_gen_limit)],
             responses={200: {"description": "Idempotent replay of an earlier request"},
                        402: {"description": "Insufficient credits"},
                        409: {"description": "Idempotency-Key reused with a different body"}})
async def create_generation(
    body: GenerationCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=8, max_length=128),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    gen, created = await generation_service.create_generation(db, user, body, idempotency_key, client_ip(request))
    if not created:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    return ok({"generation_id": gen.id, "status": gen.status.lower(), "created_at": gen.created_at})


@router.get("", response_model=Envelope[GenerationPage])
async def list_generations(
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, max_length=200),
    status_filter: str | None = Query(None, alias="status", max_length=24),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows, next_cursor = await generation_service.list_generations(db, user, limit=limit, cursor=cursor,
                                                                  status=status_filter)
    items = [await generation_service.generation_view(db, g, with_assets=True) for g in rows]
    return ok({"items": items, "next_cursor": next_cursor})


@router.get("/{generation_id}", response_model=Envelope[GenerationOut])
async def get_generation(generation_id: uuid.UUID, user: CurrentUser = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)) -> dict:
    gen = await generation_service.get_owned_generation(db, user, generation_id)
    return ok(await generation_service.generation_view(db, gen))


@router.post("/{generation_id}/cancel", response_model=Envelope[GenerationOut])
async def cancel_generation(generation_id: uuid.UUID, request: Request,
                            user: CurrentUser = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)) -> dict:
    gen = await generation_service.cancel_generation(db, user, generation_id, client_ip(request))
    return ok(await generation_service.generation_view(db, gen, with_assets=False))
