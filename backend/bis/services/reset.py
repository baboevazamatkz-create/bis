"""Очистка данных организации: чистый старт для пилота.

Удаляется всё, что накоплено: документы и файлы, база знаний организации, «Эрудит», объекты, договоры, сметы, учёт,
приём с площадки, поручения, уведомления, чаты, журнал. Остаются организация (реквизиты, настройки), пользователи
и общие нормативы. Ключи и настройки BIS (.env) не затрагиваются.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)
KEEP = {"organizations", "users", "alembic_version"}


def reset_organization(db: Session, storage, org_id: uuid.UUID) -> dict[str, Any]:
    import bis.models  # noqa: F401 — все таблицы в метаданных
    from bis.db import Base
    from bis.models import Document, IntakeEvent, KnowledgeSource, Organization

    keys: set[str] = set()
    for model in (Document, IntakeEvent, KnowledgeSource):
        keys |= set(db.scalars(select(model.file_key).where(model.organization_id == org_id, model.file_key.is_not(None))))

    counts: dict[str, int] = {}
    tables = Base.metadata.sorted_tables
    for table in reversed(tables):  # сначала зависимые
        if table.name in KEEP:
            continue
        if "organization_id" in table.c:
            n = db.execute(delete(table).where(table.c.organization_id == org_id)).rowcount
        else:  # строки без своей организации, но принадлежащие её строкам (сообщения чата и т.п.)
            n = 0
            for fk in table.foreign_keys:
                parent = fk.column.table
                if parent.name not in KEEP and "organization_id" in parent.c:
                    n += db.execute(delete(table).where(fk.parent.in_(
                        select(parent.c.id).where(parent.c.organization_id == org_id)))).rowcount
        if n:
            counts[table.name] = n
    org = db.get(Organization, org_id)
    if org is not None and (org.settings or {}).get("telegram"):  # объекты чатов Telegram ссылались на удалённое
        tg = dict(org.settings["telegram"])
        tg.pop("objects", None)
        org.settings = {**org.settings, "telegram": tg}
    db.flush()

    removed = 0
    for key in keys:
        try:
            storage.delete(key)
            removed += 1
        except Exception as e:  # noqa: BLE001 — запись в базе удалена, файл мог быть удалён раньше
            log.warning("Очистка: файл %s не удалён: %s", key, e)
    from bis.rag.search import invalidate_vectors

    invalidate_vectors()
    return {"rows": sum(counts.values()), "tables": counts, "files": removed}
