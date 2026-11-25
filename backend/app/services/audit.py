from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


def audit(db: AsyncSession, *, action: str, resource_type: str, resource_id: object | None = None,
          actor_id: uuid.UUID | None = None, actor_type: str = "user", ip: str | None = None,
          details: dict[str, Any] | None = None) -> None:
    """Adds an audit row to the current transaction (committed with the business change)."""
    db.add(AuditLog(
        actor_id=actor_id,
        actor_type=actor_type,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        ip_address=ip,
        details=details or {},
    ))
