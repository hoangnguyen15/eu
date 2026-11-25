"""Credit ledger with row-level locking.

Lock order is always: generation row -> wallet row, to avoid deadlocks.
Idempotency is enforced twice: by `generations.credit_state` transitions under
lock, and by partial unique indexes on `credit_transactions` (one RESERVE per
generation; at most one SETTLE *or* RELEASE per generation).
"""
from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InsufficientCreditsError
from app.models import CreditState, CreditTransaction, CreditTxType, CreditWallet, Generation


async def get_or_create_wallet(db: AsyncSession, user_id: uuid.UUID, *, for_update: bool = False) -> CreditWallet:
    stmt = select(CreditWallet).where(CreditWallet.user_id == user_id)
    if for_update:
        stmt = stmt.with_for_update()
    wallet = (await db.execute(stmt)).scalar_one_or_none()
    if wallet is None:
        wallet = CreditWallet(user_id=user_id, balance=0, reserved_balance=0)
        db.add(wallet)
        await db.flush()
        if for_update:
            wallet = (await db.execute(stmt)).scalar_one()
    return wallet


def _ledger(db: AsyncSession, wallet: CreditWallet, tx_type: CreditTxType, amount: int,
            generation_id: uuid.UUID | None = None, note: str | None = None) -> CreditTransaction:
    tx = CreditTransaction(
        user_id=wallet.user_id,
        generation_id=generation_id,
        transaction_type=tx_type.value,
        amount=amount,
        balance_after=wallet.balance,
        reserved_after=wallet.reserved_balance,
        note=note,
    )
    db.add(tx)
    return tx


async def grant(db: AsyncSession, user_id: uuid.UUID, amount: int, note: str | None = None) -> CreditWallet:
    if amount <= 0:
        raise ValueError("grant amount must be positive")
    wallet = await get_or_create_wallet(db, user_id, for_update=True)
    wallet.balance += amount
    _ledger(db, wallet, CreditTxType.GRANT, amount, note=note)
    await db.flush()
    return wallet


async def reserve(db: AsyncSession, gen: Generation) -> CreditWallet:
    """Reserve `gen.credit_cost` for a generation being created in the same transaction."""
    if gen.credit_state != CreditState.NONE.value:
        raise RuntimeError("credits already reserved for generation")
    # Single conditional UPDATE: takes the row lock and checks funds in one
    # round trip, which shortens lock hold time under bursts from one user.
    row = (await db.execute(
        update(CreditWallet)
        .where(CreditWallet.user_id == gen.user_id,
               CreditWallet.balance - CreditWallet.reserved_balance >= gen.credit_cost)
        .values(reserved_balance=CreditWallet.reserved_balance + gen.credit_cost)
        .returning(CreditWallet.id)
        .execution_options(synchronize_session=False)
    )).scalar_one_or_none()
    if row is None:
        wallet = await get_or_create_wallet(db, gen.user_id)
        await db.refresh(wallet)
        raise InsufficientCreditsError("Not enough credits", details={
            "required": gen.credit_cost, "available": wallet.balance - wallet.reserved_balance})
    wallet = await db.get(CreditWallet, row, populate_existing=True)
    assert wallet is not None
    gen.credit_state = CreditState.RESERVED.value
    _ledger(db, wallet, CreditTxType.RESERVE, gen.credit_cost, generation_id=gen.id)
    await db.flush()
    return wallet


async def _lock_generation(db: AsyncSession, generation_id: uuid.UUID) -> Generation:
    stmt = select(Generation).where(Generation.id == generation_id).with_for_update()
    return (await db.execute(stmt)).scalar_one()


async def settle(db: AsyncSession, gen: Generation) -> CreditWallet | None:
    """Charge reserved credits. `gen` must already be locked FOR UPDATE by caller.
    Returns None when nothing was settled (already settled/released)."""
    if gen.credit_state != CreditState.RESERVED.value:
        return None
    wallet = await get_or_create_wallet(db, gen.user_id, for_update=True)
    wallet.reserved_balance -= gen.credit_cost
    wallet.balance -= gen.credit_cost
    gen.credit_state = CreditState.SETTLED.value
    _ledger(db, wallet, CreditTxType.SETTLE, gen.credit_cost, generation_id=gen.id)
    await db.flush()
    return wallet


async def release(db: AsyncSession, gen: Generation, note: str | None = None) -> CreditWallet | None:
    """Return reserved credits. `gen` must already be locked FOR UPDATE by caller."""
    if gen.credit_state != CreditState.RESERVED.value:
        return None
    wallet = await get_or_create_wallet(db, gen.user_id, for_update=True)
    wallet.reserved_balance -= gen.credit_cost
    gen.credit_state = CreditState.RELEASED.value
    _ledger(db, wallet, CreditTxType.RELEASE, gen.credit_cost, generation_id=gen.id, note=note)
    await db.flush()
    return wallet


def wallet_view(wallet: CreditWallet) -> dict[str, int]:
    return {
        "balance": wallet.balance,
        "reserved_balance": wallet.reserved_balance,
        "available_balance": wallet.balance - wallet.reserved_balance,
    }
