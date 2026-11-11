"""Folder connections, sync status, source review and one-time form approval."""
from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from bis.api.deps import require
from bis.db import get_db
from bis.models import ConstructionObject, Document, Organization, User
from bis.models.libraries import LibraryConnection, LibraryFile, LibraryVersion
from bis.services import library_forms as forms
from bis.services import library_sync as sync
from bis.services.audit import audit
from bis.services.storage import get_storage

router = APIRouter(prefix='/libraries', tags=['libraries'])


class ConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    path: str = Field(min_length=1, max_length=2000)
    level: Literal['knowledge', 'organization', 'project']
    object_id: uuid.UUID | None = None


class PolicyIn(BaseModel):
    strict: bool


class Approval(BaseModel):
    kind: Literal['aosr', 'aook', 'letter', 'work_journal', 'id_registry']
    mapping: forms.Mapping


class Assignment(BaseModel):
    level: Literal['organization', 'project']
    object_id: uuid.UUID | None = None


def owned(db, user, connection_id, *, write=False, content=False):
    c = db.get(LibraryConnection, connection_id)
    if c is None or c.organization_id not in (None, user.organization_id):
        raise HTTPException(404, 'Подключение не найдено')
    if write and c.level == 'knowledge' and not user.is_platform_admin:
        raise HTTPException(403, 'Общую библиотеку настраивает администратор платформы')
    scope = db.info.get('source_context')
    if content and scope and scope.strict and c.level == 'project' and c.object_id != scope.object_id:
        raise HTTPException(404, 'Выберите проект этой библиотеки')
    return c


def version_owned(db, user, version_id, *, write=False):
    v = db.get(LibraryVersion, version_id)
    item = db.get(LibraryFile, v.file_id) if v else None
    if item is None:
        raise HTTPException(404, 'Версия не найдена')
    c = owned(db, user, item.connection_id, write=write, content=True)
    if write and (item.active_version_id != v.id or item.state != 'ready'):
        raise HTTPException(409, 'Можно подтверждать только текущую прочитанную версию')
    return v, item, c


def out(c):
    return {k: getattr(c, k) for k in ('id', 'level', 'organization_id', 'object_id', 'name', 'path', 'enabled', 'status', 'error', 'synced_at')}


@router.get('')
def connections(db: Session = Depends(get_db), user: User = Depends(require('knowledge:read'))):
    rows = db.scalars(select(LibraryConnection).where(or_(LibraryConnection.organization_id.is_(None),
                      LibraryConnection.organization_id == user.organization_id)).order_by(LibraryConnection.created_at))
    org = db.get(Organization, user.organization_id)
    from bis.config import get_settings

    return {'strict': bool((org.settings or {}).get('libraries_strict')), 'connections': [out(c) for c in rows],
            'allowed_roots': [str(p) for p in get_settings().library_roots] if user.role == 'admin' else [],
            'form_fields': forms.FIELDS, 'platform_admin': user.is_platform_admin}


@router.put('/policy')
def policy(body: PolicyIn, db: Session = Depends(get_db), user: User = Depends(require('settings:write'))):
    org = db.get(Organization, user.organization_id)
    org.settings = {**(org.settings or {}), 'libraries_strict': body.strict}
    audit(db, user.organization_id, actor_type='user', actor=str(user.id), action='library.policy', details={'strict': body.strict})
    db.commit()
    return {'strict': body.strict}


@router.post('', status_code=201)
def connect(body: ConnectionIn, tasks: BackgroundTasks, db: Session = Depends(get_db),
            user: User = Depends(require('settings:write'))):
    if body.level == 'knowledge' and not user.is_platform_admin:
        raise HTTPException(403, 'Общую библиотеку подключает администратор платформы')
    if (body.level == 'project') != bool(body.object_id):
        raise HTTPException(400, 'Только библиотека проекта должна иметь объект')
    if body.object_id:
        obj = db.get(ConstructionObject, body.object_id)
        if obj is None or obj.organization_id != user.organization_id:
            raise HTTPException(404, 'Объект не найден')
    try:
        path = sync.checked_path(body.path)
    except PermissionError:
        import json
        import os

        if not os.environ.get('BIS_LOCAL_DIR') or user.role != 'admin':
            raise
        from bis.config import get_settings
        from bis.services.local_config import save

        path = Path(body.path).expanduser().resolve()
        if not path.is_dir():
            raise HTTPException(400, 'Папка не найдена на компьютере с BIS') from None
        roots = [str(p) for p in get_settings().library_roots]
        save({'BIS_LIBRARY_ROOTS': json.dumps([*roots, str(path)], ensure_ascii=False)})
    c = LibraryConnection(name=body.name, path=str(path), level=body.level, object_id=body.object_id,
                          organization_id=None if body.level == 'knowledge' else user.organization_id)
    db.add(c)
    db.flush()
    audit(db, user.organization_id, actor_type='user', actor=str(user.id), action='library.connect',
          entity_type='library_connection', entity_id=c.id, details={'level': body.level})
    db.commit()
    tasks.add_task(sync.execute, c.id)
    return out(c)


@router.post('/{connection_id}/sync', status_code=202)
def synchronize(connection_id: uuid.UUID, tasks: BackgroundTasks, force: bool = False, db: Session = Depends(get_db),
                user: User = Depends(require('settings:write'))):
    c = owned(db, user, connection_id, write=True)
    if not c.enabled:
        raise HTTPException(409, 'Подключение отключено')
    tasks.add_task(sync.execute, c.id, force=force)
    return {'status': 'queued'}


@router.delete('/{connection_id}/contents')
def clear_contents(connection_id: uuid.UUID, db: Session = Depends(get_db),
                   user: User = Depends(require('settings:write'))):
    """Clear only imported copies for this connection. Never touch the mounted originals."""
    from sqlalchemy import delete

    c = owned(db, user, connection_id, write=True, content=True)
    with sync.connection_lease(c.id) as acquired:
        if not acquired:
            raise HTTPException(409, 'База сейчас обрабатывается; повторите очистку после завершения')
        locked = db.scalar(select(LibraryConnection).where(LibraryConnection.id == c.id)
                           .with_for_update(skip_locked=True))
        if locked is None:
            raise HTTPException(409, 'База сейчас обрабатывается; повторите очистку после завершения')
        versions = list(db.scalars(select(LibraryVersion).join(LibraryFile, LibraryFile.id == LibraryVersion.file_id)
                                  .where(LibraryFile.connection_id == c.id)))
        source_ids = [v.source_id for v in versions if v.source_id]
        document_ids = [v.document_id for v in versions if v.document_id]
        from bis.models import KnowledgeSource

        # Database cascades invalidate source references; generated acts and originals remain.
        db.execute(delete(LibraryFile).where(LibraryFile.connection_id == c.id))
        if source_ids:
            db.execute(delete(KnowledgeSource).where(KnowledgeSource.id.in_(source_ids)))
        if document_ids:
            db.execute(delete(Document).where(Document.id.in_(document_ids), Document.source == 'library'))
        c.enabled, c.status, c.error, c.synced_at = False, 'empty', None, None
        audit(db, user.organization_id, actor_type='user', actor=str(user.id), action='library.clear',
              entity_type='library_connection', entity_id=c.id, details={'versions': len(versions)})
        db.commit()
        from bis.rag.search import invalidate_vectors

        invalidate_vectors()
        return {'cleared_versions': len(versions), 'enabled': False, 'originals_preserved': True}

@router.patch('/{connection_id}')
def toggle(connection_id: uuid.UUID, enabled: bool, db: Session = Depends(get_db),
           user: User = Depends(require('settings:write'))):
    c = owned(db, user, connection_id, write=True)
    c.enabled = enabled
    db.commit()
    return out(c)


@router.get('/{connection_id}/files')
def files(connection_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require('knowledge:read'))):
    owned(db, user, connection_id, content=True)
    result = []
    for f in db.scalars(select(LibraryFile).where(LibraryFile.connection_id == connection_id).order_by(LibraryFile.relative_path)):
        versions = db.scalars(select(LibraryVersion).where(LibraryVersion.file_id == f.id).order_by(LibraryVersion.revision.desc())).all()
        active = next((v for v in versions if v.id == f.active_version_id), None)
        from bis.models import KnowledgeSource

        source = db.get(KnowledgeSource, active.source_id) if active and active.source_id else None
        reading = (source.meta or {}).get('reading') or {} if source else {}
        partial = f.state == 'ready' and (reading.get('warnings') or reading.get('empty_pages'))
        result.append({'id': f.id, 'path': f.relative_path, 'state': 'partial' if partial else f.state,
            'error': f.error or '; '.join(reading.get('warnings', [])) or None, 'reading': reading,
            'source_id': str(source.id) if source else None, 'source_kind': source.kind if source else None,
            'source_status': source.status if source else None,
            'active_version_id': f.active_version_id, 'versions': [
                {'id': v.id, 'revision': v.revision, 'sha256': v.sha256, 'form_kind': v.form_kind,
                 'mapping': v.mapping, 'approved_at': v.approved_at, 'source_id': v.source_id,
                 'created_at': v.created_at} for v in versions]})
    return result


@router.get('/versions/{version_id}/file')
def original(version_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require('knowledge:read'))):
    v, item, _ = version_owned(db, user, version_id)
    return Response(get_storage().get(v.file_key), media_type=v.mime or 'application/octet-stream',
                    headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(Path(item.relative_path).name)})


@router.post('/versions/{version_id}/prepare')
def prepare(version_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require('settings:write'))):
    v, item, _ = version_owned(db, user, version_id, write=True)
    if v.approved_at:
        raise HTTPException(409, 'Эта версия уже подтверждена; сначала отмените подтверждение')
    from bis.llm.gateway import get_gateway
    try:
        mapping = forms.prepare(get_storage().get(v.file_key), item.relative_path, get_gateway())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    v.mapping = mapping.model_dump()
    db.commit()
    fmt, places = forms.targets(get_storage().get(v.file_key), item.relative_path)
    return {**v.mapping, "targets": places}


@router.post('/versions/{version_id}/preview')
def preview(version_id: uuid.UUID, body: Approval, db: Session = Depends(get_db),
            user: User = Depends(require('settings:write'))):
    v, item, _ = version_owned(db, user, version_id, write=True)
    try:
        raw = get_storage().get(v.file_key)
        forms.validate_mapping(raw, item.relative_path, body.mapping, body.kind)
        content = forms.filled(raw, item.relative_path, body.mapping, {k: 'ПРИМЕР: ' + label for k, label in forms.FIELDS.items()})
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(content, media_type=v.mime or 'application/octet-stream')


@router.post('/versions/{version_id}/approve')
def approve(version_id: uuid.UUID, body: Approval, db: Session = Depends(get_db),
            user: User = Depends(require('settings:write'))):
    v, item, _ = version_owned(db, user, version_id, write=True)
    if v.approved_at:
        raise HTTPException(409, 'Карта этой версии уже подтверждена')
    try:
        forms.validate_mapping(get_storage().get(v.file_key), item.relative_path, body.mapping, body.kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    v.form_kind, v.mapping, v.approved_at, v.approved_by_id = body.kind, body.mapping.model_dump(), dt.datetime.now(dt.UTC), user.id
    audit(db, user.organization_id, actor_type='user', actor=str(user.id), action='library.form_approve',
          entity_type='library_version', entity_id=v.id, details={'sha256': v.sha256, 'kind': body.kind})
    db.commit()
    return {'approved_at': v.approved_at}


@router.post('/versions/{version_id}/revoke')
def revoke(version_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require('settings:write'))):
    v, _, _ = version_owned(db, user, version_id, write=True)
    v.approved_at, v.approved_by_id = None, None
    audit(db, user.organization_id, actor_type='user', actor=str(user.id), action='library.form_revoke', entity_id=v.id)
    db.commit()
    return {'approved_at': None}


@router.get('/unassigned/list')
def unassigned(db: Session = Depends(get_db), user: User = Depends(require('settings:write'))):
    rows = db.scalars(select(Document).where(Document.organization_id == user.organization_id,
        Document.object_id.is_(None), Document.data['library_scope'].astext.is_(None))
        .execution_options(library_management=True).order_by(Document.created_at.desc()).limit(500))
    return [{'id': d.id, 'title': d.title, 'kind': d.kind} for d in rows]


@router.post('/unassigned/{document_id}')
def assign(document_id: uuid.UUID, body: Assignment, db: Session = Depends(get_db),
           user: User = Depends(require('settings:write'))):
    doc = db.scalar(select(Document).where(Document.id == document_id, Document.organization_id == user.organization_id)
                    .execution_options(library_management=True))
    if doc is None or (doc.data or {}).get('library_version_id'):
        raise HTTPException(404, 'Документ не найден или управляется подключением папки')
    if (body.level == 'project') != bool(body.object_id):
        raise HTTPException(400, 'Укажите объект только для базы проекта')
    if body.object_id:
        obj = db.get(ConstructionObject, body.object_id)
        if obj is None or obj.organization_id != user.organization_id:
            raise HTTPException(404, 'Объект не найден')
    doc.object_id, doc.data = body.object_id, {**(doc.data or {}), 'library_scope': body.level}
    audit(db, user.organization_id, actor_type='user', actor=str(user.id), action='library.assign', entity_id=doc.id,
          details=body.model_dump(mode='json'))
    db.commit()
    return {'id': doc.id}
