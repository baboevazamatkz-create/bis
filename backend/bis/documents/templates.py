"""Шаблоны организаций (DOCX с полями {{ ... }} в синтаксисе docxtpl/Jinja2).

Если у организации есть шаблон по умолчанию для вида документа — он используется вместо встроенной формы.
В шаблон передаются поля модели данных и готовые строки (`*_text`).
"""

from __future__ import annotations

import io
import uuid
from typing import Any

from docxtpl import DocxTemplate
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.documents.common import fmt_date, fmt_date_long
from bis.models import DocumentTemplate
from bis.services.storage import Storage


def template_context(data: BaseModel) -> dict[str, Any]:
    ctx = data.model_dump(mode="python")
    for key, value in list(ctx.items()):
        if hasattr(value, "strftime"):
            ctx[f"{key}_text"] = fmt_date(value)
            ctx[f"{key}_long"] = fmt_date_long(value)
    for key, value in data:
        if hasattr(value, "format") and isinstance(value, BaseModel):
            ctx[f"{key}_text"] = value.format()
    return ctx


def render_template(template_bytes: bytes, context: dict[str, Any]) -> bytes:
    tpl = DocxTemplate(io.BytesIO(template_bytes))
    tpl.render(context, autoescape=True)
    buf = io.BytesIO()
    tpl.save(buf)
    return buf.getvalue()


def default_template(db: Session, org_id: uuid.UUID, kind: str) -> DocumentTemplate | None:
    return db.scalar(
        select(DocumentTemplate).where(
            DocumentTemplate.organization_id == org_id,
            DocumentTemplate.kind == kind,
            DocumentTemplate.is_default.is_(True),
        )
    )


def render_with_org_template(db: Session, storage: Storage, org_id: uuid.UUID, kind: str, data: BaseModel) -> bytes | None:
    from bis.services.library_access import strict_enabled

    if strict_enabled(db, org_id):
        from bis.services.library_forms import render

        return render(db, storage, org_id, kind, data)
    tpl = default_template(db, org_id, kind)
    if tpl is None:
        return None
    return render_template(storage.get(tpl.file_key), template_context(data))
