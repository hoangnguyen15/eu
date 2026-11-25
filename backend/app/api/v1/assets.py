from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.exceptions import NotFoundError
from app.models import Asset
from app.schemas import AssetOut, Envelope, ok
from app.services.generation_service import asset_view
from app.storage import s3
from app.storage.images import EXTENSIONS

router = APIRouter(prefix="/assets", tags=["assets"])


async def _owned_asset(db: AsyncSession, user: CurrentUser, asset_id: uuid.UUID) -> Asset:
    asset = await db.get(Asset, asset_id)
    if asset is None or asset.user_id != user.id:
        raise NotFoundError("Asset not found")
    return asset


@router.get("/{asset_id}", response_model=Envelope[AssetOut])
async def get_asset(asset_id: uuid.UUID, user: CurrentUser = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)) -> dict:
    return ok(asset_view(await _owned_asset(db, user, asset_id)))


@router.get("/{asset_id}/download", status_code=307, response_class=RedirectResponse,
            responses={307: {"description": "Redirect to a short-lived signed URL on the private bucket"}})
async def download_asset(asset_id: uuid.UUID, user: CurrentUser = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    asset = await _owned_asset(db, user, asset_id)
    name = f"{asset.generation_id}-{asset.image_index + 1}.{EXTENSIONS.get(asset.mime_type, 'bin')}"
    url = s3.presign_get(asset.storage_key, download_name=name)
    return RedirectResponse(url, status_code=307, headers={"Cache-Control": "no-store"})
