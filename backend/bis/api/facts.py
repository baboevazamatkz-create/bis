"""Project fact ledger and reproducible, evidence-first investigations."""
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
from bis.db import get_db
from bis.models import User
from bis.models.facts import BrainInvestigation, FactConflict, FactDecision, FactSnapshot
from bis.services import project_facts as ledger

router = APIRouter(prefix='/brain/facts', tags=['project facts'])


class RefreshIn(BaseModel):
    object_id: uuid.UUID | None = None
    limit: int = Field(20, ge=1, le=80)
    force: bool = False


class InvestigationIn(BaseModel):
    question: str = Field(min_length=3, max_length=20000)
    object_id: uuid.UUID | None = None
    budget: int = Field(12, ge=1, le=50)


class DecisionIn(BaseModel):
    fact_id: uuid.UUID
    revision: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=2000)


def project_context(db, user, object_id=None):
    from bis.agents.resolve import AmbiguousReference
    ctx = agent_context(db, user)
    try:
        project_id = ledger.scope(ctx, object_id)
    except AmbiguousReference as exc:
        raise HTTPException(409, {'message': 'Выберите проект из найденных', 'candidates': exc.candidates}) from exc
    return ctx, project_id


@router.get('')
def facts(object_id: uuid.UUID | None = None, predicate: str | None = None, location: str | None = None,
          work: str | None = None, category: str | None = None, limit: int = 200, db: Session = Depends(get_db),
          user: User = Depends(require('documents:read'))):
    ctx, pid = project_context(db, user, object_id)
    result = ledger.query(ctx, pid, predicate=predicate, location=location, work=work, category_filter=category, limit=max(1, min(limit, 1000)))
    docs = db.scalars(ledger.source_documents(ctx, pid)).all()
    snapshots = db.scalars(select(FactSnapshot).where(FactSnapshot.organization_id == user.organization_id,
        FactSnapshot.object_id == pid, FactSnapshot.state.in_(['active', 'partial', 'unread', 'stale']))).all()
    return {**result, 'object_id': str(pid), 'labels': ledger.LABELS,
        'coverage': {'documents': len(docs), 'indexed_documents': len({s.document_id for s in snapshots if s.document_id and s.state == 'active'
            and any(d.id == s.document_id and d.file_key == s.file_key and d.kind == s.kind for d in docs)}),
                     'unread': [s.title for s in snapshots if s.state == 'unread'],
                     'partial': [s.title for s in snapshots if s.state == 'partial'],
                     'stale': [s.title for s in snapshots if s.state == 'stale']}}


@router.post('/refresh')
def refresh(body: RefreshIn, db: Session = Depends(get_db), user: User = Depends(require('documents:write'))):
    ctx, pid = project_context(db, user, body.object_id)
    result = ledger.refresh_project(ctx, pid, limit=body.limit, force=body.force)
    db.commit()
    return result


@router.get('/conflicts')
def conflicts(object_id: uuid.UUID | None = None, db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    ctx, pid = project_context(db, user, object_id)
    rows = db.scalars(select(FactConflict).where(FactConflict.organization_id == ctx.org_id,
        FactConflict.object_id == pid, FactConflict.state != 'stale').order_by(FactConflict.created_at)).all()
    # GET never reconciles or changes decisions. Signature refresh is performed by extraction/investigation.
    return ledger.conflicts_out(ctx, rows)


@router.post('/conflicts/{conflict_id}/decision')
def decide(conflict_id: uuid.UUID, body: DecisionIn, db: Session = Depends(get_db),
           user: User = Depends(require('documents:write'))):
    ctx = agent_context(db, user)
    try:
        result = ledger.decide(ctx, conflict_id, body.fact_id, body.revision, body.reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return ledger.conflicts_out(ctx, [result])[0]


@router.get('/conflicts/{conflict_id}/history')
def history(conflict_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    ctx = agent_context(db, user)
    conflict = db.get(FactConflict, conflict_id)
    if conflict is None or conflict.organization_id != user.organization_id:
        raise HTTPException(404, 'Противоречие не найдено')
    ledger.scope(ctx, conflict.object_id)
    return [{'id': d.id, 'fact_id': d.fact_id, 'reason': d.reason, 'revision': d.revision,
             'user_id': d.user_id, 'at': d.created_at} for d in db.scalars(select(FactDecision).where(
        FactDecision.conflict_id == conflict.id).order_by(FactDecision.revision))]


@router.post('/investigate')
def investigate(body: InvestigationIn, db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    from bis.brain.orchestrator import answer, investigate
    ctx, pid = project_context(db, user, body.object_id)
    result = investigate(ctx, body.question, pid, budget=body.budget)
    text = answer(ctx, result)
    db.commit()
    return {**result, 'text': text, 'citations': ctx.citations}


@router.get('/investigations')
def investigations(object_id: uuid.UUID | None = None, db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    ctx, pid = project_context(db, user, object_id)
    rows = db.scalars(select(BrainInvestigation).where(BrainInvestigation.organization_id == ctx.org_id,
        BrainInvestigation.object_id == pid).order_by(BrainInvestigation.created_at.desc()).limit(30)).all()
    return [{'id': row.id, 'question': row.question, 'state': row.state, 'plan': row.plan,
             'coverage': row.result.get('coverage'), 'missing': row.result.get('missing'), 'created_at': row.created_at} for row in rows]


@router.get('/investigations/{investigation_id}')
def investigation_detail(investigation_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    ctx = agent_context(db, user)
    row = db.get(BrainInvestigation, investigation_id)
    if row is None or row.organization_id != user.organization_id:
        raise HTTPException(404, 'Проверка не найдена')
    ledger.scope(ctx, row.object_id)
    return row.result


@router.get('/snapshots/{snapshot_id}/file')
def evidence_file(snapshot_id: uuid.UUID, object_id: uuid.UUID | None = None, db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    ctx = agent_context(db, user)
    if object_id:
        ledger.scope(ctx, object_id)
    snapshot = db.get(FactSnapshot, snapshot_id)
    if snapshot is None or snapshot.organization_id != user.organization_id:
        raise HTTPException(404, 'Основание не найдено')
    ledger.scope(ctx, snapshot.object_id)
    if not snapshot.file_key:
        return {'title': snapshot.title, 'method': snapshot.method, 'details': snapshot.details,
                'facts': ledger.snapshot_claims(ctx, snapshot).model_dump(mode='json')}
    try:
        raw = ctx.storage.get(snapshot.file_key)
    except OSError as exc:
        raise HTTPException(404, 'Сохранённый оригинал недоступен') from exc
    if snapshot.file_sha256 and hashlib.sha256(raw).hexdigest() != snapshot.file_sha256:
        raise HTTPException(409, 'Файл основания изменился; цитата сохранена в реестре')
    return Response(raw, media_type=mimetypes.guess_type(snapshot.file_name or '')[0] or 'application/octet-stream',
        headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(snapshot.file_name or 'source')})
