"""Folder connections, sync status, source review and one-time form approval."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from bis.api.deps import require
from bis.config import get_settings
from bis.db import get_db
from bis.models import ConstructionObject, Document, Organization, User
from bis.models.libraries import LibraryConnection, LibraryFile, LibraryVersion
from bis.security import has_scope
from bis.services import docx_review
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


class FillContext(BaseModel):
    object_id: uuid.UUID | None = None
    act_date: dt.date | None = None
    values: dict[str, str] = Field(default_factory=dict, max_length=200)
    selections: dict[str, str] = Field(default_factory=dict, max_length=200)


class Approval(BaseModel):
    kind: Literal['aosr', 'aook', 'letter', 'work_journal', 'id_registry']
    mapping: forms.Mapping
    preview_token: str = Field(default='', max_length=64)
    context: FillContext | None = None


class Assignment(BaseModel):
    level: Literal['organization', 'project']
    object_id: uuid.UUID | None = None


def can_manage_knowledge(user):
    return user.is_platform_admin or (bool(os.environ.get('BIS_LOCAL_DIR')) and user.role == 'admin')


def owned(db, user, connection_id, *, write=False, content=False):
    c = db.get(LibraryConnection, connection_id)
    if c is None or c.organization_id not in (None, user.organization_id):
        raise HTTPException(404, 'Подключение не найдено')
    if write and c.level == 'knowledge' and not can_manage_knowledge(user):
        raise HTTPException(403, 'Общую библиотеку настраивает администратор платформы')
    scope = db.info.get('source_context')
    if content and scope and scope.strict and c.level == 'project' and c.object_id != scope.object_id:
        raise HTTPException(404, 'Выберите проект этой библиотеки')
    return c


def version_owned(db, user, version_id, *, write=False, lock=False):
    v = db.get(LibraryVersion, version_id, with_for_update=lock, populate_existing=lock)
    item = db.get(LibraryFile, v.file_id, populate_existing=lock) if v else None
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
            'form_fields': forms.FIELDS, 'platform_admin': can_manage_knowledge(user)}


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
    if body.level == 'knowledge' and not can_manage_knowledge(user):
        raise HTTPException(403, 'Общую библиотеку подключает администратор платформы')
    if (body.level == 'project') != bool(body.object_id):
        raise HTTPException(400, 'Только библиотека проекта должна иметь объект')
    if body.object_id:
        obj = db.get(ConstructionObject, body.object_id)
        if obj is None or obj.organization_id != user.organization_id:
            raise HTTPException(404, 'Объект не найден')
    body.path = body.path.strip().strip('"')
    try:
        path = sync.checked_path(body.path)
    except PermissionError:
        import json
        import os

        if not os.environ.get('BIS_LOCAL_DIR') or user.role != 'admin':
            raise HTTPException(403, 'Папка вне разрешённых каталогов сервера') from None
        from bis.config import get_settings
        from bis.services.local_config import save

        path = Path(body.path).expanduser().resolve()
        if not path.is_dir():
            raise HTTPException(400, 'Папка не найдена на компьютере с BIS') from None
        roots = [str(p) for p in get_settings().library_roots]
        save({'BIS_LIBRARY_ROOTS': json.dumps([*roots, str(path)], ensure_ascii=False)})
    if not path.is_dir():
        raise HTTPException(400, 'Папка не найдена на компьютере с BIS')
    c = LibraryConnection(name=body.name, path=str(path), level=body.level, object_id=body.object_id,
                          organization_id=None if body.level == 'knowledge' else user.organization_id)
    db.add(c)
    db.flush()
    audit(db, user.organization_id, actor_type='user', actor=str(user.id), action='library.connect',
          entity_type='library_connection', entity_id=c.id, details={'level': body.level})
    db.commit()
    tasks.add_task(sync.execute, c.id)
    return out(c)




@router.post('/upload', status_code=201)
def upload_library(tasks: BackgroundTasks, files: list[UploadFile] = File(...),
                   level: Literal['knowledge', 'organization', 'project'] = Form(...),
                   object_id: uuid.UUID | None = Form(None), db: Session = Depends(get_db),
                   user: User = Depends(require('settings:write'))):
    from bis.config import get_settings

    if level == 'knowledge' and not can_manage_knowledge(user):
        raise HTTPException(403, 'Общую библиотеку подключает администратор платформы')
    if (level == 'project') != bool(object_id):
        raise HTTPException(400, 'Выберите объект для базы проекта')
    if object_id:
        obj = db.get(ConstructionObject, object_id)
        if obj is None or obj.organization_id != user.organization_id:
            raise HTTPException(404, 'Объект не найден')
    roots = get_settings().library_roots
    if not roots:
        raise HTTPException(400, 'Не настроена папка хранения библиотек BIS_LIBRARY_ROOTS')
    if len(files) > 1000:
        raise HTTPException(400, 'Загружайте не более 1000 файлов за один раз')
    target = Path(roots[0]).resolve() / 'uploads' / str(user.organization_id) / str(uuid.uuid4())
    total = 0
    names = set()
    try:
        for file in files:
            name = (file.filename or '').replace('\\', '/')
            parts = name.split('/')
            if any(p in ('', '.', '..') or ':' in p for p in parts):
                raise HTTPException(400, 'Недопустимое имя файла')
            if Path(name).suffix.lower() not in sync.SUPPORTED:
                raise HTTPException(400, f'Формат файла не поддерживается: {name}')
            if name.casefold() in names:
                raise HTTPException(400, f'Повторное имя файла: {name}')
            names.add(name.casefold())
            dest = target.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with dest.open('xb') as output:
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if size > sync.MAX_FILE or total > 500 * 1024 * 1024:
                        raise HTTPException(413, 'Лимит: 100 МБ на файл, 500 МБ на загрузку')
                    output.write(chunk)
        return connect(ConnectionIn(name=files[0].filename.split('/')[0] if len(files) == 1 else f'Загрузка · {len(files)} файлов',
                       path=str(target), level=level, object_id=object_id), tasks, db, user)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    finally:
        for file in files:
            file.file.close()


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
def prepare(version_id: uuid.UUID, kind: Literal['aosr', 'aook', 'letter'] | None = None, example: bool = False,
            db: Session = Depends(get_db), user: User = Depends(require('settings:write'))):
    v, item, connection = version_owned(db, user, version_id, write=True)
    _check_review_project(db, connection)
    if v.approved_at:
        raise HTTPException(409, 'Эта версия уже подтверждена; сначала отмените подтверждение')
    from bis.llm.gateway import get_gateway
    try:
        raw = get_storage().get(v.file_key)
        chosen = kind or ('aosr' if v.form_kind == 'aosr' else None)
        if example or (chosen in forms.DOCX_KINDS and _is_docx(item)):
            chosen = chosen or 'aosr'
            if example and chosen != 'aosr':
                raise ValueError('Настройка по заполненному примеру поддерживает АОСР в DOCX')
            check_template_version(raw, v)
            labels: dict[str, str] = {}
            if example:
                mapping, origins, places = forms.prepare_aosr_example(raw, item.relative_path)
                confirmed = _confirmed_mapping(db, connection, chosen, raw, example=True)
                if confirmed:
                    mapping = confirmed
                    origins = dict.fromkeys((entry.target for entry in mapping.entries), 'learned')
            else:
                confirmed = _confirmed_mapping(db, connection, chosen, raw)
                mapping, origins, places = forms.prepare_docx(raw, item.relative_path, chosen, get_gateway,
                                                              target_labels=labels, confirmed=confirmed)
            return _review_payload(raw, chosen, mapping, places, origins, labels, example=example)
        if kind is not None:
            raise ValueError('Это действие предназначено для DOCX')
        mapping = forms.prepare(raw, item.relative_path, get_gateway())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    v, item, _ = version_owned(db, user, version_id, write=True, lock=True)
    if v.approved_at:
        raise HTTPException(409, 'Эта версия уже подтверждена; сначала отмените подтверждение')
    v.mapping = mapping.model_dump(exclude_none=True)
    db.commit()
    _, places = forms.targets(raw, item.relative_path)
    return {**v.mapping, "targets": places}


def _is_docx(item):
    return item.relative_path.lower().endswith('.docx')


def _semantic_docx(item, kind, mapping):
    return _is_docx(item) and (kind == 'aosr' or mapping.profile == 'docx_fields')


def _review_payload(raw, kind, mapping, places, origins, labels, *, example=False):
    return {**mapping.model_dump(exclude_none=True), 'kind': kind, 'targets': places, 'origins': origins, 'example': example,
            'fields': forms.docx_fields(kind, example=example), 'target_labels': labels,
            'required': sorted(forms.REQUIRED[kind]),
            'alternatives': {'representatives': sorted(forms.REPRESENTATIVE_FIELDS)} if kind in ('aosr', 'aook') else {},
            'modes': {target: 'span' if example else forms.blank_target_mode(target, anchor)
                      for target, anchor in places.items()},
            'layout': docx_review.outline(raw, precise=mapping.profile in forms.DOCX_PROFILES)}


def _confirmed_mapping(db, connection, kind, raw, *, example=False):
    signature = docx_review.fingerprint(raw, example=example)
    scope = [LibraryConnection.enabled.is_(True), LibraryConnection.level == connection.level]
    if connection.level == 'knowledge':
        scope.append(LibraryConnection.id == connection.id)
    else:
        scope.extend((LibraryConnection.organization_id == connection.organization_id,
                      LibraryConnection.object_id == connection.object_id))
    saved = db.scalars(select(LibraryVersion.mapping).join(LibraryFile, LibraryFile.id == LibraryVersion.file_id)
                       .join(LibraryConnection, LibraryConnection.id == LibraryFile.connection_id)
                       .where(*scope, LibraryVersion.form_kind == kind, LibraryVersion.approved_at.is_not(None),
                              LibraryVersion.mapping['layout_key'].astext == signature)).all()
    candidates = {}
    for payload in saved:
        mapping = forms.Mapping.model_validate(payload)
        if example == any(entry.mode == 'span' for entry in mapping.entries):
            key = tuple(sorted(entry.model_dump_json() for entry in mapping.entries))
            candidates[key] = mapping
    return next(iter(candidates.values())) if len(candidates) == 1 else None


@router.get('/versions/{version_id}/review')
def review(version_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require('knowledge:read'))):
    version, item, connection = version_owned(db, user, version_id)
    _check_review_project(db, connection)
    if not version.approved_at or version.form_kind not in forms.DOCX_KINDS or not _is_docx(item):
        raise HTTPException(404, 'У этой версии нет утверждённой карты DOCX')
    raw = get_storage().get(version.file_key)
    mapping = forms.Mapping.model_validate(version.mapping)
    try:
        check_template_version(raw, version)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    places = {entry.target: entry.anchor for entry in mapping.entries}
    example = any(entry.mode == 'span' for entry in mapping.entries)
    return _review_payload(raw, version.form_kind, mapping, places, {}, {}, example=example)


class Resolution(FillContext):
    kind: Literal['aosr', 'aook', 'letter']
    mapping: forms.Mapping


def _check_review_project(db, connection):
    scope = db.info.get('source_context')
    if scope and scope.object_id and connection.level == 'project' and connection.object_id != scope.object_id:
        raise HTTPException(404, 'Выберите проект этой библиотеки')


def _effective_act_date(mapping, context):
    from bis.services.executive_pipeline import dates_in

    dates = set()
    for entry in mapping.entries:
        if entry.field != 'date' or entry.target not in context.values:
            continue
        parsed = dates_in(context.values[entry.target])
        if len(parsed) != 1:
            raise ValueError('Укажите дату документа в формате ДД.ММ.ГГГГ')
        dates.add(parsed[0][1])
    if context.act_date:
        dates.add(context.act_date)
    if len(dates) > 1:
        raise ValueError('В карте указаны разные даты документа')
    return next(iter(dates), None)


def _resolve_context(db, user, connection, mapping, context, *, as_of=None):
    from bis.services import form_sources

    _check_review_project(db, connection)
    current_scope = db.info.get('source_context')
    object_id = context.object_id or connection.object_id or (current_scope.object_id if current_scope else None)
    if connection.object_id is not None and object_id != connection.object_id:
        raise LookupError('Утверждённая форма относится к другому проекту')
    return form_sources.resolve(db, user, mapping, object_id=object_id, act_date=_effective_act_date(mapping, context),
                                values=context.values, selections=context.selections, as_of=as_of), object_id


@router.post('/versions/{version_id}/resolve')
def resolve(version_id: uuid.UUID, body: Resolution, db: Session = Depends(get_db),
            user: User = Depends(require('documents:write'))):
    version, item, connection = version_owned(db, user, version_id)
    if not _is_docx(item):
        raise HTTPException(400, 'Заполнение по источникам поддерживает DOCX')
    mapping = forms.Mapping.model_validate(version.mapping) if version.approved_at else body.mapping
    if version.approved_at and version.form_kind != body.kind:
        raise HTTPException(409, 'Вид утверждённой формы изменился')
    raw = get_storage().get(version.file_key)
    try:
        check_template_version(raw, version)
        forms.validate_mapping(raw, item.relative_path, mapping, body.kind, require_complete=False)
        resolved, _ = _resolve_context(db, user, connection, mapping, body)
        return resolved
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _render_resolved(raw, item, mapping, resolved):
    values = {target: result['value'] for target, result in resolved['fields'].items()}
    content = forms.filled(raw, item.relative_path, mapping, {}, target_values=values)
    if mapping.profile in forms.DOCX_PROFILES:
        docx_review.verify_geometry(raw, content)
    return content


def _fill_issues(resolved, kind):
    from bis.services.executive_pipeline import dates_in

    labels = forms.docx_fields(kind, example=kind == 'aosr')
    required = set(forms.REQUIRED[kind])
    results = list(resolved['fields'].values())
    if 'representatives' in required and not any(row['field'] == 'representatives' for row in results):
        required.update(forms.REPRESENTATIVE_FIELDS)
    issues = []
    dates = {}
    for result in results:
        field = result['field']
        if result['state'] == 'resolved':
            if kind != 'letter' and field in ('work_start', 'work_end', 'date'):
                parsed = dates_in(result['value'])
                if len(parsed) != 1 or (field in dates and dates[field] != parsed[0][1]):
                    issues.append({'level': 'error', 'field': field, 'message': 'Проверьте дату: ' + labels[field]})
                else:
                    dates[field] = parsed[0][1]
            continue
        missing = result['state'] == 'missing'
        message = ('Нет данных: ' if missing else 'Требуется выбор: ') + labels.get(field, field)
        if result.get('issues'):
            message += ' · ' + ', '.join(result['issues'])
        issues.append({'level': 'warning' if missing and field not in required else 'error', 'field': field, 'message': message})
    for first, last in (('work_start', 'work_end'), ('work_end', 'date')):
        if first in dates and last in dates and dates[first] > dates[last]:
            issues.append({'level': 'error', 'field': last, 'message': f'{labels[last]} раньше: {labels[first]}'})
    return issues


@router.post('/versions/{version_id}/generate')
def generate(version_id: uuid.UUID, body: FillContext, db: Session = Depends(get_db),
             user: User = Depends(require('documents:write'))):
    from bis.services.documents import save_document

    version, item, connection = version_owned(db, user, version_id)
    kind = version.form_kind
    if not version.approved_at or kind not in forms.DOCX_KINDS or not _is_docx(item):
        raise HTTPException(409, 'Сначала утвердите карту DOCX')
    if not connection.enabled or item.active_version_id != version.id or item.state != 'ready' or version.state != 'ready':
        raise HTTPException(409, 'Выбранная версия шаблона уже не действует')
    raw = get_storage().get(version.file_key)
    mapping = forms.Mapping.model_validate(version.mapping)
    try:
        check_template_version(raw, version)
        forms.validate_mapping(raw, item.relative_path, mapping, kind)
        as_of = dt.date.today()
        resolved, object_id = _resolve_context(db, user, connection, mapping, body, as_of=as_of)
        conflicts = [target for target, row in resolved['fields'].items() if row['state'] == 'conflict']
        if conflicts:
            labels = forms.docx_fields(kind)
            raise HTTPException(409, 'Требуется выбор: ' + ', '.join(labels.get(resolved['fields'][t]['field'], t) for t in conflicts))
        issues = _fill_issues(resolved, kind)
        content = _render_resolved(raw, item, mapping, resolved)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    stem = Path(item.relative_path).stem
    name = stem + ' — заполнено.docx'
    mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    document = save_document(db, get_storage(), org_id=user.organization_id, kind=kind, title=stem,
                             data={'form_resolution': resolved, 'form_fill': {**body.model_dump(mode='json', exclude={'object_id'}),
                                   'as_of': as_of.isoformat()}, 'library_form': {'version_id': str(version.id),
                                   'sha256': version.sha256}}, file_bytes=content, file_name=name, mime=mime,
                             created_by=f'user:{user.id}', object_id=object_id, doc_date=_effective_act_date(mapping, body),
                             issues=issues)
    db.commit()
    return Response(content, media_type=mime, headers={
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name),
        'X-BIS-Document-Id': str(document.id),
        'X-BIS-Document-Issues': quote(json.dumps(issues[:12], ensure_ascii=False)),
    })


def check_template_version(raw, version):
    if hashlib.sha256(raw).hexdigest() != version.sha256:
        raise ValueError('Сохранённый оригинал формы изменился')


def form_preview_token(version, user, mapping, kind):
    payload = f'docx-preview:{kind}:{version.id}:{version.sha256}:{user.id}:{mapping.model_dump_json()}'
    return hmac.new(get_settings().secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()


@router.post('/versions/{version_id}/preview')
def preview(version_id: uuid.UUID, body: Approval, db: Session = Depends(get_db),
            user: User = Depends(require('documents:write'))):
    v, item, connection = version_owned(db, user, version_id)
    _check_review_project(db, connection)
    proposed = body.mapping
    if v.approved_at:
        if v.form_kind != body.kind:
            raise HTTPException(409, 'Вид утверждённой формы изменился')
        proposed = forms.Mapping.model_validate(v.mapping)
    else:
        if not has_scope(user.role, 'settings:write'):
            raise HTTPException(403, 'Настройка карты доступна администратору')
        v, item, connection = version_owned(db, user, version_id, write=True)
    headers = {}
    try:
        raw = get_storage().get(v.file_key)
        if _semantic_docx(item, body.kind, proposed) or (v.approved_at and _is_docx(item) and body.kind in forms.DOCX_KINDS):
            check_template_version(raw, v)
            mapping = proposed.model_copy(update={'profile': 'aosr_docx'}) if body.kind == 'aosr' and not v.approved_at else proposed
            if body.context is not None:
                forms.validate_mapping(raw, item.relative_path, mapping, body.kind)
                resolved, _ = _resolve_context(db, user, connection, mapping, body.context)
                content = _render_resolved(raw, item, mapping, resolved)
                headers['X-BIS-Document-Issues'] = quote(json.dumps(_fill_issues(resolved, body.kind)[:12], ensure_ascii=False))
            elif mapping.profile in forms.DOCX_PROFILES:
                content = forms.preview_docx(raw, item.relative_path, mapping, body.kind)
                docx_review.verify_geometry(raw, content)
            else:
                forms.validate_mapping(raw, item.relative_path, mapping, body.kind)
                content = forms.filled(raw, item.relative_path, mapping, {k: 'ПРИМЕР: ' + label for k, label in forms.FIELDS.items()})
            headers['X-BIS-Form-Preview'] = form_preview_token(v, user, mapping, body.kind)
        else:
            forms.validate_mapping(raw, item.relative_path, proposed, body.kind)
            content = forms.filled(raw, item.relative_path, proposed, {k: 'ПРИМЕР: ' + label for k, label in forms.FIELDS.items()})
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(content, media_type=v.mime or 'application/octet-stream', headers=headers)


@router.post('/versions/{version_id}/approve')
def approve(version_id: uuid.UUID, body: Approval, db: Session = Depends(get_db),
            user: User = Depends(require('settings:write'))):
    v, item, connection = version_owned(db, user, version_id, write=True, lock=True)
    _check_review_project(db, connection)
    if v.approved_at:
        raise HTTPException(409, 'Карта этой версии уже подтверждена')
    mapping = body.mapping
    try:
        raw = get_storage().get(v.file_key)
        if _semantic_docx(item, body.kind, mapping):
            check_template_version(raw, v)
            if body.kind == 'aosr':
                mapping = mapping.model_copy(update={'profile': 'aosr_docx'})
            if not hmac.compare_digest(body.preview_token.encode(), form_preview_token(v, user, mapping, body.kind).encode()):
                raise ValueError('Скачайте пробное заполнение для текущей карты полей')
        else:
            forms.validate_mapping(raw, item.relative_path, mapping, body.kind)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    payload = mapping.model_dump(exclude_none=True)
    if mapping.profile in forms.DOCX_PROFILES:
        payload['layout_key'] = docx_review.fingerprint(raw, example=any(entry.mode == 'span' for entry in mapping.entries))
    v.form_kind, v.mapping, v.approved_at, v.approved_by_id = body.kind, payload, dt.datetime.now(dt.UTC), user.id
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
