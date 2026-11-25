from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountSession, Asset, Generation, GoogleAccount, TaskLease
from app.storage import s3
from app.workers.protocol import MsgType, message


async def build_offer(db: AsyncSession, lease: TaskLease, gen: Generation) -> dict[str, Any]:
    profile_ref = None
    account_email = None
    if lease.account_session_id:
        sess = await db.get(AccountSession, lease.account_session_id)
        if sess is not None:
            profile_ref = sess.profile_ref
            acct = await db.get(GoogleAccount, sess.account_id)
            account_email = acct.google_email if acct else None
    ref_urls: list[str] = []
    if gen.reference_asset_ids:
        ids = [uuid.UUID(x) for x in gen.reference_asset_ids]
        assets = (await db.execute(select(Asset).where(Asset.id.in_(ids), Asset.user_id == gen.user_id))).scalars()
        ref_urls = [s3.presign_get(a.storage_key) for a in assets]
    return message(MsgType.TASK_OFFER, {
        "task_id": str(lease.id),
        "generation_id": str(gen.id),
        "attempt": lease.attempt,
        "slot_index": lease.slot_index,
        "provider": gen.provider,
        "prompt": gen.prompt,
        "aspect_ratio": gen.aspect_ratio,
        "number_of_images": gen.number_of_images,
        "reference_image_urls": ref_urls,
        "profile_ref": profile_ref,
        "account_email": account_email,
        "offer_expires_at": lease.expires_at.isoformat(),
    })
