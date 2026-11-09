"""Project sheets, grounded specifications and act-to-design comparisons."""
from __future__ import annotations

import hashlib
import mimetypes
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.api.deps import agent_context, require
from bis.api.facts import project_context
from bis.db import get_db
from bis.models import Document, User
from bis.models.project_reading import ProjectReading
from bis.services import project_alignment as alignment
from bis.services import project_reading as reader
from bis.services.library_access import SourceContext, document_predicate
from bis.services.project_facts import SourceInProgress, scope

router = APIRouter(prefix='/projects', tags=['project understanding'])


class ReadIn(BaseModel):
    budget: int = Field(4, ge=1, le=20)
    with_vision: bool = False
    force: bool = False


@router.get('/documents')
def documents(object_id: uuid.UUID | None = None, db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    ctx, pid = project_context(db, user, object_id)
    docs = db.scalars(select(Document).where(Document.object_id == pid, Document.kind == 'project_doc',
        document_predicate(SourceContext(ctx.org_id, pid, True)), Document.status.notin_(['cancelled', 'replaced', 'rejected']))
        .order_by(Document.created_at, Document.id)).all()
    result = []
    for doc in docs:
        try:
            current = reader.current(ctx, doc.id)
        except (OSError, ValueError):
            current = None
        result.append({'id': doc.id, 'title': doc.title, 'file_name': doc.file_name,
            'reading_id': str(current.id) if current else None, 'state': current.state if current else 'pending',
            'coverage': current.payload['coverage'] if current else None})
    return result


@router.post('/documents/{document_id}/read')
def read(document_id: uuid.UUID, body: ReadIn, db: Session = Depends(get_db), user: User = Depends(require('documents:write'))):
    ctx = agent_context(db, user)
    try:
        row = reader.read(ctx, document_id, budget=body.budget, with_vision=body.with_vision, force=body.force)
    except SourceInProgress as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return reader.view(row)


@router.get('/readings/{reading_id}')
def get_reading(reading_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    row = db.get(ProjectReading, reading_id)
    if row is None or row.organization_id != user.organization_id:
        raise HTTPException(404, 'Чтение проекта не найдено')
    scope(agent_context(db, user), row.object_id)
    return reader.view(row)


@router.get('/readings/{reading_id}/file')
def original(reading_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    ctx = agent_context(db, user)
    row = db.get(ProjectReading, reading_id)
    if row is None or row.organization_id != ctx.org_id:
        raise HTTPException(404, 'Основание проекта не найдено')
    scope(ctx, row.object_id)
    try:
        raw = ctx.storage.get(row.file_key)
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(404, 'Оригинал этой версии недоступен') from exc
    if hashlib.sha256(raw).hexdigest() != row.file_sha256:
        raise HTTPException(409, 'Файл исходной версии изменился')
    return Response(raw, media_type=mimetypes.guess_type(row.file_name or '')[0] or 'application/octet-stream',
        headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(row.file_name or 'project')})


@router.get('/items')
def items(object_id: uuid.UUID | None = None, location: str | None = None, element: str | None = None,
          limit: int = 200, db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    ctx, pid = project_context(db, user, object_id)
    docs = db.scalars(select(Document).where(Document.object_id == pid, Document.kind == 'project_doc',
        document_predicate(SourceContext(ctx.org_id, pid, True)), Document.status.notin_(['cancelled', 'replaced', 'rejected']))).all()
    readings, pending = [], []
    for doc in docs:
        try:
            row = reader.current(ctx, doc.id)
        except (ValueError, OSError):
            row = None
        if row:
            readings.append(row)
        if not row or row.state != 'ready':
            pending.append(str(doc.id))
    rows = alignment.design_items(readings)
    if location:
        rows = [r for r in rows if alignment.scope_matches(r['location'] or '', location)]
    if element:
        rows = [r for r in rows if alignment.norm(r['element']) == alignment.norm(element)]
    limit = max(1, min(limit, 1000))
    return {'items': rows[:limit], 'truncated': len(rows) > limit, 'pending_documents': pending,
        'note': 'Отсутствие строки при неполном чтении не доказывает отсутствие позиции в проекте.'}


@router.post('/acts/{document_id}/compare')
def compare(document_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    ctx = agent_context(db, user)
    doc = db.get(Document, document_id)
    if doc is None or doc.organization_id != user.organization_id or doc.kind not in ('aosr', 'aook') or not doc.object_id:
        raise HTTPException(404, 'Акт не найден')
    result = alignment.compare_act(ctx, doc)
    return result
