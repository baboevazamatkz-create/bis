"""Six-section pilot: source-backed lists and transparent reading status."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from bis.api.deps import require
from bis.api.schemas import DocumentOut
from bis.db import get_db
from bis.models import Contract, Document, KnowledgeChunk, KnowledgeSource, User

router = APIRouter(prefix='/workspace', tags=['workspace'])


@router.get('/documents')
def documents(category: Literal['documents', 'schemes', 'project', 'contracts'], limit: int = 200,
              offset: int = 0, q: str | None = Query(None, max_length=300), db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    scope = db.info.get('source_context')
    project = scope.object_id if scope else None
    kinds = {'documents': ['aosr', 'aook', 'id_registry', 'work_journal'], 'schemes': ['scheme', 'survey'],
             'project': ['project_doc', 'other'], 'contracts': ['contract']}[category]
    stmt = select(Document).where(Document.organization_id == user.organization_id, Document.kind.in_(kinds),
        Document.status != 'replaced', or_(Document.object_id == project if project else False,
            Document.object_id.is_(None)))
    if q and q.strip():
        query = q.strip()
        matching = select(KnowledgeSource.document_id).join(KnowledgeChunk, KnowledgeChunk.source_id == KnowledgeSource.id).where(
            KnowledgeChunk.content.icontains(query, autoescape=True))
        stmt = stmt.where(or_(Document.title.icontains(query, autoescape=True),
            Document.file_name.icontains(query, autoescape=True), Document.number.icontains(query, autoescape=True),
            Document.id.in_(matching)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.scalars(stmt.order_by(Document.created_at.desc()).offset(max(0, offset)).limit(min(500, max(1, limit)))))
    ids = [d.id for d in rows]
    counts = dict(db.execute(select(KnowledgeSource.document_id, func.count(KnowledgeChunk.id))
        .join(KnowledgeChunk, KnowledgeChunk.source_id == KnowledgeSource.id)
        .where(KnowledgeSource.document_id.in_(ids)).group_by(KnowledgeSource.document_id)).all()) if ids else {}
    contract_types = dict(db.execute(select(Contract.document_id, Contract.kind).where(
        Contract.organization_id == user.organization_id, Contract.document_id.in_(ids))).all()) if ids else {}
    result = []
    for doc in rows:
        item = DocumentOut.model_validate(doc).model_dump(mode='json')
        reading = dict((doc.data or {}).get('reading') or {})
        if (doc.data or {}).get('reading_error'):
            reading['warnings'] = [*(reading.get('warnings') or []), doc.data['reading_error']]
        indexed = counts.get(doc.id, 0)
        state = 'indexed' if indexed else 'unread'
        if indexed and (reading.get('empty_pages') or reading.get('warnings') or reading.get('complete') is False):
            state = 'partial'
        if (doc.data or {}).get('read_state') in ('queued', 'running'):
            state = 'reading'
        item['reading_status'] = state
        item['chunks'] = indexed
        item['reading'] = reading
        item['contract_kind'] = contract_types.get(doc.id) or (doc.data or {}).get('contract_kind') or 'unclassified'
        result.append(item)
    return {'documents': result, 'total': total, 'has_more': offset + len(rows) < total,
            'needs_project': project is None, 'project_id': str(project) if project else None}


@router.get('/reading-status')
def reading_status(user: User = Depends(require('knowledge:read'))):
    from bis import ocr
    from bis.config import get_settings
    from bis.llm.gateway import get_gateway

    settings = get_settings()
    try:
        gateway = get_gateway()
        providers = [{'name': name, 'capabilities': list(entry.capabilities)} for name, entry in gateway.entries.items()
                     if getattr(entry.provider, 'model', '') not in ('', 'offline')]
        text_configured = bool(gateway.has_capability('text', 'chat'))
        vision_configured = bool(gateway.has_capability('vision', 'vision'))
        error = None
    except Exception as exc:  # noqa: BLE001 — diagnostics must remain usable with broken provider settings
        providers, error = [], f'Ошибка настройки модели: {type(exc).__name__}'
        text_configured, vision_configured = False, False
    folder, key = settings.yandex()
    from bis import voice

    return {'providers': providers, 'model_error': error, 'ocr_configured': ocr.available() and bool(key),
            'text_configured': text_configured, 'vision_configured': vision_configured,
            'ocr_provider': settings.ocr_provider, 'yandex_folder_set': bool(folder),
            'folder_id': folder, 'local_setup_available': bool(__import__('os').environ.get('BIS_LOCAL_DIR')),
            'voice': voice.status(),
            'note': 'Наличие настроек не подтверждает доступ к облаку. Проверка выполняется отдельно.'}


@router.get('/scheme-needs')
def scheme_needs(db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    from bis.services.id_check import check_archive

    scope = db.info.get('source_context')
    if not scope or not scope.object_id:
        return {'items': [], 'needs_project': True}
    checked = check_archive(db, user.organization_id, scope.object_id)
    items = []
    for act in checked['items']:
        missing = [r for r in act['refs'] if r['kind'] == 'scheme' and not r['found']]
        if any('Нет ссылки на исполнительную схему' in text for text in act['issues']):
            missing.append({'number': None})
        for ref in missing:
            items.append({'document_id': act['document_id'], 'title': act['title'], 'works': act['works'],
                          'number': ref['number'], 'reason': 'Не найдена схема по ссылке акта' if ref['number'] else 'В акте не указана схема'})
    return {'items': items, 'checked_acts': checked['acts'], 'needs_project': False}


def imported_scope(db, user, level):
    scope = db.info.get('source_context')
    project = scope.object_id if scope else None
    if level == 'project' and not project:
        raise HTTPException(409, 'Выберите проект')
    return select(Document).where(Document.organization_id == user.organization_id,
        Document.file_key.is_not(None), Document.data['library_version_id'].astext.is_(None),
        or_(Document.source.in_(['archive', 'uploaded']), Document.kind == 'contract'),
        Document.object_id == project if level == 'project' else Document.object_id.is_(None))


@router.get('/imports')
def imports(level: Literal['organization', 'project'], db: Session = Depends(get_db),
            user: User = Depends(require('documents:read'))):
    rows = list(db.scalars(imported_scope(db, user, level).order_by(Document.created_at.desc())))
    return {'total': len(rows), 'documents': [{'id': str(d.id), 'title': d.title,
        'state': (d.data or {}).get('read_state', 'stored'), 'error': (d.data or {}).get('reading_error')}
        for d in rows[:200]], 'has_more': len(rows) > 200}


@router.post('/imports/reread', status_code=202)
def reread_imports(level: Literal['organization', 'project'], tasks: BackgroundTasks, db: Session = Depends(get_db),
                   user: User = Depends(require('documents:write'))):
    from bis.services.source_reread import process_many, queue_documents

    ids = queue_documents(db, list(db.scalars(imported_scope(db, user, level))))
    tasks.add_task(process_many, ids)
    return {'queued': len(ids)}


@router.delete('/imports')
def clear_imports(level: Literal['organization', 'project'], db: Session = Depends(get_db),
                  user: User = Depends(require('settings:write'))):
    from sqlalchemy import delete

    from bis.services.audit import audit

    docs = list(db.scalars(imported_scope(db, user, level)))
    ids = [d.id for d in docs]
    # Imported sources and their derived contract records only; keep generated acts and users/projects.
    db.execute(delete(KnowledgeSource).where(KnowledgeSource.document_id.in_(ids)))
    db.execute(delete(Contract).where(Contract.organization_id == user.organization_id, Contract.document_id.in_(ids)))
    db.execute(delete(Document).where(Document.id.in_(ids), Document.organization_id == user.organization_id))
    audit(db, user.organization_id, actor_type='user', actor=str(user.id), action='workspace.clear_imports',
          details={'level': level, 'documents': len(ids)})
    db.commit()
    from bis.rag.search import invalidate_vectors

    invalidate_vectors()
    return {'cleared_documents': len(ids), 'generated_documents_preserved': True}


class AISetup(BaseModel):
    folder_id: str = Field(pattern=r'^[A-Za-z0-9_-]+$', max_length=100)
    api_key: SecretStr | None = None
    model: str = Field(default='yandexgpt/latest', pattern=r'^[A-Za-z0-9_./-]+$', max_length=100)
    ocr: bool = True
    speech: bool = True
    vision_model: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_./-]+$', max_length=100)


@router.post('/ai-setup')
def ai_setup(body: AISetup, user: User = Depends(require('settings:write'))):
    """Local pilot settings only; return no credentials and preserve unrelated .env entries."""
    import json
    import os
    from pathlib import Path

    from bis.config import get_settings
    from bis.llm.gateway import set_gateway

    home = os.environ.get('BIS_LOCAL_DIR')
    if not home:
        raise HTTPException(409, 'Настройка через интерфейс доступна при локальном запуске BIS-Pilot')
    root = Path(home)
    envfile = root / '.env'
    if not envfile.is_file():
        raise HTTPException(409, 'Не найден файл настроек локального пилота')
    key = body.api_key.get_secret_value().strip() if body.api_key else get_settings().yandex()[1]
    if not key or any(c.isspace() for c in key):
        raise HTTPException(422, 'Введите действующий API-ключ без пробелов')
    config = root / 'config' / 'llm.pilot.json'
    config.parent.mkdir(parents=True, exist_ok=True)
    configured = get_settings()
    try:
        previous_providers, previous_routes = configured.llm_providers(), configured.llm_routes()
    except (ValueError, OSError):
        previous_providers, previous_routes = [], {}
    vision_providers = [p.model_dump(exclude_none=True) for p in previous_providers
                        if 'vision' in p.capabilities and p.kind != 'offline']
    renamed = {}
    for provider in vision_providers:
        if provider['name'] == 'yandexgpt':
            provider['name'] = 'yandexgpt-vision'
            renamed['yandexgpt'] = 'yandexgpt-vision'
        if (provider.get('base_url') or '').startswith('https://llm.api.cloud.yandex.net'):
            model = provider.get('model', '')
            if model.startswith('gpt://'):
                provider['model'] = 'gpt://' + body.folder_id + '/' + model[6:].split('/', 1)[-1]
            provider.pop('api_key', None)
            provider['api_key_env'] = 'YANDEX_API_KEY'
    vision_names = [p['name'] for p in vision_providers]
    vision_order = [renamed.get(name, name) for name in previous_routes.get('vision', vision_names)
                    if renamed.get(name, name) in vision_names]
    if body.vision_model:
        vision_providers = [p for p in vision_providers if p['name'] != 'pilot-vision']
        vision_providers.append({'name': 'pilot-vision', 'kind': 'openai_compat',
            'base_url': 'https://llm.api.cloud.yandex.net/v1', 'model': f'gpt://{body.folder_id}/{body.vision_model}',
            'api_key_env': 'YANDEX_API_KEY', 'auth_scheme': 'Api-Key', 'capabilities': ['text', 'vision'],
            'max_sensitivity': 'internal', 'timeout': 90, 'temperature': 0.1})
        vision_order = ['pilot-vision', *(name for name in vision_order if name != 'pilot-vision')]
    routes = {'default': ['yandexgpt', 'offline']}
    if vision_order:
        routes['vision'] = vision_order
    config.write_text(json.dumps({'providers': [
        {'name': 'yandexgpt', 'kind': 'openai_compat', 'base_url': 'https://llm.api.cloud.yandex.net/v1',
         'model': f'gpt://{body.folder_id}/{body.model}', 'api_key_env': 'YANDEX_API_KEY', 'auth_scheme': 'Api-Key',
         'max_sensitivity': 'internal', 'mask_pii': True, 'temperature': 0.1, 'timeout': 60},
        *vision_providers, {'name': 'offline', 'kind': 'offline', 'max_sensitivity': 'confidential'}],
        'routes': routes}, ensure_ascii=False, indent=2), encoding='utf-8')
    values = {'YANDEX_FOLDER_ID': body.folder_id, 'BIS_YANDEX_FOLDER_ID': body.folder_id, 'YANDEX_API_KEY': key,
              'BIS_LLM_CONFIG_FILE': config.as_posix(), 'BIS_OCR_PROVIDER': 'yandex' if body.ocr else 'none',
              'BIS_STT_PROVIDER': 'yandex' if body.speech else 'openai'}
    from bis.services.local_config import save

    save(values)
    set_gateway(None)
    return {'saved': True, 'requires_restart': False,
            'message': 'Настройки сохранены. Проверьте ответ модели; OCR и голос проверяются на файле и записи.'}
