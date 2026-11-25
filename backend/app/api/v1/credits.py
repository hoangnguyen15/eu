from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user
from app.core.exceptions import NotFoundError
from app.models import CreditTransaction, User
from app.schemas import CreditBalance, CreditTransactionOut, Envelope, UserProfile, ok
from app.services import credit_service

router = APIRouter(tags=["user", "credits"])


@router.get("/user/profile", response_model=Envelope[UserProfile])
async def profile(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(User, user.id)
    if row is None:
        raise NotFoundError("User not found")
    return ok({"id": row.id, "email": row.email, "role": row.role, "status": row.status,
               "created_at": row.created_at})


@router.get("/user/credits", response_model=Envelope[CreditBalance])
async def credits(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    wallet = await credit_service.get_or_create_wallet(db, user.id)
    await db.commit()
    return ok(credit_service.wallet_view(wallet))


@router.get("/credits/transactions", response_model=Envelope[list[CreditTransactionOut]])
async def transactions(limit: int = Query(50, ge=1, le=200), user: CurrentUser = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)) -> dict:
    rows = (await db.execute(
        select(CreditTransaction).where(CreditTransaction.user_id == user.id)
        .order_by(CreditTransaction.created_at.desc()).limit(limit)
    )).scalars().all()
    return ok([{c: getattr(r, c) for c in CreditTransactionOut.model_fields} for r in rows])
