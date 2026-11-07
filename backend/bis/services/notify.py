"""Уведомления: в интерфейсе и (если настроено) в Telegram. Идемпотентны по ключу события."""

from __future__ import annotations

import logging
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from bis.config import get_settings
from bis.models import Notification, Person, User

log = logging.getLogger(__name__)
LEVEL_ICONS = {"info": "ℹ️", "warning": "⚠️", "critical": "🔴"}


def managers(db: Session, org_id: uuid.UUID, roles: tuple[str, ...] = ("admin", "manager")) -> list[User]:
    return list(db.scalars(select(User).where(User.organization_id == org_id, User.role.in_(roles),
                                              User.is_active.is_(True))))


def users_for_person(db: Session, person_id: uuid.UUID | None) -> list[User]:
    if person_id is None:
        return []
    person = db.get(Person, person_id)
    if person is None:
        return []
    if person.user_id:
        user = db.get(User, person.user_id)
        return [user] if user and user.is_active else []
    if person.email:
        user = db.scalar(select(User).where(User.organization_id == person.organization_id,
                                            User.email == person.email.lower()))
        return [user] if user and user.is_active else []
    return []


def notify(
    db: Session,
    org_id: uuid.UUID,
    users: list[User],
    *,
    title: str,
    body: str | None = None,
    level: str = "info",
    entity_type: str | None = None,
    entity_id: str | None = None,
    key: str | None = None,
) -> int:
    """Создаёт уведомления; с одинаковым key повторно не создаёт. Возвращает число новых уведомлений."""
    created = 0
    seen: set[uuid.UUID] = set()
    for user in users:
        if user.id in seen:
            continue
        seen.add(user.id)
        stmt = insert(Notification).values(
            id=uuid.uuid4(), organization_id=org_id, user_id=user.id, level=level, title=title, body=body,
            entity_type=entity_type, entity_id=str(entity_id) if entity_id else None,
            key=f"{key}:{user.id}" if key else None, is_read=False, channels={},
        ).on_conflict_do_nothing(index_elements=["organization_id", "key"]).returning(Notification.id)
        new_id = db.execute(stmt).scalar()
        if new_id is None:
            continue
        created += 1
        if user.telegram_chat_id:
            status = send_telegram(user.telegram_chat_id, f"{LEVEL_ICONS.get(level, '')} {title}\n{body or ''}".strip())
            db.get(Notification, new_id).channels = {"telegram": status}
    return created


def send_telegram(chat_id: str, text: str) -> str:
    token = get_settings().telegram_bot_token
    if not token:
        return "disabled"
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat_id, "text": text[:4000]}, timeout=10)
        return "sent" if r.status_code == 200 else f"error {r.status_code}"
    except httpx.HTTPError as e:
        log.warning("Telegram: %s", e)
        return "error"
