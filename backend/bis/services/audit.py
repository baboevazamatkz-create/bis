"""Аудит действий пользователей и агентов."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from bis.models import AuditLog


def audit(
    db: Session,
    org_id: uuid.UUID,
    *,
    actor_type: str,
    actor: str,
    action: str,
    entity_type: str | None = None,
    entity_id: Any = None,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            organization_id=org_id,
            actor_type=actor_type,
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            details=details or {},
        )
    )
