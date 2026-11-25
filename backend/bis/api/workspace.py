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
from bis.models import ConstructionObject, Contract, Document, KnowledgeChunk, KnowledgeSource, User
from bis.security import has_scope
from bis.services import project_participants

router = APIRouter(prefix='/workspace', tags=['workspace'])


def _active_project(db, user):
    scope = db.info.get('source_context')
    if not scope or not scope.object_id:
        raise HTTPException(409, 'Выберите проект')
    project = db.scalar(select(ConstructionObject).where(
        ConstructionObject.id == scope.object_id, ConstructionObject.organization_id == user.organization_id))
    if project is None:
        raise HTTPException(404, 'Проект не найден')
    return project


@router.get('/project-structure')
def project_structure(db: Session = Depends(get_db), user: User = Depends(require('registry:read'))):
    project = _active_project(db, user)
    return {**project_participants.proposal(db, project),
            'can_edit': has_scope(user.role, 'registry:write'), 'can_edit_organization': has_scope(user.role, 'settings:write')}


@router.put('/project-structure')
def confirm_structure(body: project_participants.Confirmation, db: Session = Depends(get_db),
                      user: User = Depends(require('registry:write'))):
    project = _active_project(db, user)
    edit_org = has_scope(user.role, 'settings:write')
    try:
        project = project_participants.confirm(db, project, body, user, edit_org=edit_org)
        result = project_participants.proposal(db, project)
    except project_participants.StaleStructure as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return {**result, 'can_edit': True, 'can_edit_organization': edit_org}


@router.get('/id-structure')
def id_structure(db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    from bis.services.id_work_index import declared_works, work_index
    from bis.services.library_access import document_predicate

    scope = db.info.get('source_context')
    if scope is None or scope.object_id is None:
        return {'needs_project': True, 'areas': [], 'acts': [], 'unassigned_act_ids': [], 'indexed_documents': 0}

    scoped = (
        document_predicate(scope), Document.object_id == scope.object_id,
        Document.status.notin_(['cancelled', 'replaced', 'rejected']),
    )
    act_docs = db.scalars(select(Document).where(*scoped, Document.kind.in_(['aosr', 'aook']))
                          .order_by(Document.created_at.desc(), Document.id)).all()
    act_text = {doc.id: set() for doc in act_docs if not (doc.data or {}).get('works')}
    index_query = select(Document.id, KnowledgeChunk.page, KnowledgeChunk.content).join(
        KnowledgeSource, KnowledgeSource.document_id == Document.id
    ).join(KnowledgeChunk, KnowledgeChunk.source_id == KnowledgeSource.id).where(
        *scoped, KnowledgeSource.organization_id == user.organization_id,
        KnowledgeSource.status.notin_(['cancelled', 'replaced']),
        func.coalesce(KnowledgeSource.meta['vision'].astext, 'false') != 'true',
    )
    if act_text:
        for doc_id, _, content in db.execute(index_query.where(Document.id.in_(act_text))):
            act_text[doc_id].update(declared_works(content))

    acts = []
    for doc in act_docs:
        indexed = act_text.get(doc.id, set())
        work = (doc.data or {}).get('works') or (next(iter(indexed)) if len(indexed) == 1 else '')
        acts.append({'id': str(doc.id), 'title': doc.title, 'status': doc.status,
                     'file_name': doc.file_name, 'works': work if isinstance(work, str) else ''})

    indexed_docs = set()

    def project_chunks():
        rows = db.execute(index_query.where(Document.kind == 'project_doc')
                          .order_by(Document.id, KnowledgeChunk.page, KnowledgeChunk.ord)
                          .execution_options(yield_per=128))
        for doc_id, page, content in rows:
            indexed_docs.add(doc_id)
            yield str(doc_id), page, content

    index = work_index(project_chunks(), acts)
    return {**index, 'acts': [{k: v for k, v in act.items() if k != 'works'} for act in acts],
            'indexed_documents': len(indexed_docs), 'needs_project': False}


@router.get('/documents')
def documents(category: Literal['documents', 'schemes', 'project', 'contracts'], limit: int = 200,
              offset: int = 0, q: str | None = Query(None, max_length=300), db: Session = Depends(get_db), user: User = Depends(require('documents:read'))):
    scope = db.info.get('source_context')
    project = scope.object_id if scope else None
    kinds = {'documents': ['letter', 'order', 'memo', 'instruction', 'protocol', 'administrative', 'aosr', 'aook', 'id_registry', 'work_journal'], 'schemes': ['scheme', 'survey'],
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
    return _reading_status()


def _reading_status():
    import os

    from bis.config import get_settings
    from bis.llm import gateway as gateway_module

    settings = get_settings()
    try:
        configs = {p.name: p for p in settings.llm_providers() if p.kind != 'offline'}
        routes = settings.llm_routes()
        names = routes.get('chat') or routes.get('default') or list(configs)
        selected_provider = next((name for name in names if name in configs and configs[name].kind != 'offline'), None)
        vision_names = routes.get('vision') or routes.get('default') or list(configs)
        providers = [{'name': p.name, 'capabilities': p.capabilities} for p in configs.values()]
        text_configured = any(name in configs and 'text' in configs[name].capabilities for name in names)
        vision_configured = any(name in configs and 'vision' in configs[name].capabilities for name in vision_names)
        error = None
    except Exception as exc:  # noqa: BLE001 — settings remain accessible even if a provider cannot start
        providers, error, selected_provider = [], f'Ошибка настройки модели: {type(exc).__name__}', None
        text_configured, vision_configured = False, False
    folder, key = settings.yandex()
    selected_config = configs.get(selected_provider) if not error else None
    cached_gateway = gateway_module._gateway
    from bis import voice

    return {'providers': providers, 'model_error': error,
            'ocr_configured': (settings.ocr_provider == 'yandex' and bool(folder and key)) or
                              (settings.ocr_provider in ('anthropic', 'openai') and bool(selected_config and
                                  'vision' in selected_config.capabilities and selected_config.resolved_api_key())),
            'text_configured': text_configured, 'vision_configured': vision_configured,
            'ocr_provider': settings.ocr_provider, 'yandex_folder_set': bool(folder),
            'key_configured': bool(key), 'last_model_attempts': list(cached_gateway.health.copy().values()) if cached_gateway else [],
            'selected_provider': selected_provider,
            'selected_model': selected_config.model if selected_config else None,
            'selected_kind': selected_config.kind if selected_config else None,
            'selected_base_url': selected_config.base_url if selected_config else None,
            'provider_key_configured': bool(selected_config and selected_config.resolved_api_key()),
            'folder_id': folder, 'local_setup_available': bool(os.environ.get('BIS_LOCAL_DIR')),
            'voice': voice.status(), 'note': 'Настройки и доступность сервиса проверяются отдельно.'}


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


class AIProviderSetup(BaseModel):
    provider: Literal['anthropic', 'openai', 'ollama', 'compatible', 'gigachat', 'yandex']
    api_key: SecretStr | None = None
    model: str = Field(default='', pattern=r'^[A-Za-z0-9_.:/-]*$', max_length=160)
    base_url: str | None = Field(default=None, max_length=500)
    folder_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]+$', max_length=100)
    scope: Literal['GIGACHAT_API_PERS', 'GIGACHAT_API_B2B', 'GIGACHAT_API_CORP'] = 'GIGACHAT_API_CORP'


@router.post('/ai-provider')
def ai_provider_setup(body: AIProviderSetup, user: User = Depends(require('settings:write')),
                      db: Session = Depends(get_db)):
    """Select a model while keeping independent STT and OCR credentials intact."""
    from bis.services.local_config import LOCK

    db.close()  # saving local files must not retain the authentication database connection
    with LOCK:
        return _save_ai_provider(body)


def _save_ai_provider(body: AIProviderSetup):
    import json
    import os
    import uuid
    from pathlib import Path

    from bis.config import get_settings
    from bis.llm.gateway import set_gateway
    from bis.services.local_config import save

    home = os.environ.get('BIS_LOCAL_DIR')
    if not home:
        raise HTTPException(409, 'Настройка через интерфейс доступна при локальном запуске BIS-Pilot')
    root = Path(home)
    if not (root / '.env').is_file():
        raise HTTPException(409, 'Не найден файл настроек локального пилота')

    supplied_key = body.api_key.get_secret_value().strip() if body.api_key else ''
    if supplied_key.startswith('sk-ant-') and body.provider != 'anthropic':
        raise HTTPException(422, 'Это ключ Anthropic. Выберите провайдера Anthropic · Claude.')
    if supplied_key.startswith(('sk-proj-', 'sk-svcacct-')) and body.provider != 'openai':
        raise HTTPException(422, 'Это ключ OpenAI. Выберите провайдера OpenAI.')
    if body.api_key and not supplied_key:
        raise HTTPException(422, 'API-ключ пустой')

    presets = {
        'anthropic': ('anthropic', 'Claude', body.model or 'claude-opus-5-5', None),
        'openai': ('openai_compat', 'OpenAI', body.model or 'gpt-4.1-mini', 'https://api.openai.com/v1'),
        'ollama': ('openai_compat', 'Ollama', body.model or 'qwen3:8b', body.base_url or 'http://127.0.0.1:11434/v1'),
        'compatible': ('openai_compat', 'Совместимый API', body.model, body.base_url),
        'gigachat': ('gigachat', 'GigaChat', body.model or 'GigaChat-2-Max', body.base_url),
        'yandex': ('openai_compat', 'YandexGPT', body.model or 'yandexgpt/latest', 'https://llm.api.cloud.yandex.net/v1'),
    }
    kind, label, model, base_url = presets[body.provider]
    if body.provider == 'yandex' and not body.folder_id:
        raise HTTPException(422, 'Для YandexGPT укажите Folder ID')
    if body.provider in ('compatible', 'ollama') and not base_url:
        raise HTTPException(422, 'Укажите адрес API модели')
    if body.provider == 'compatible' and not model:
        raise HTTPException(422, 'Укажите идентификатор модели')
    if body.provider == 'yandex':
        model = f'gpt://{body.folder_id}/{model}'
    key_env = {'anthropic': 'BIS_ANTHROPIC_API_KEY', 'openai': 'BIS_OPENAI_API_KEY',
               'compatible': 'BIS_COMPATIBLE_API_KEY', 'gigachat': 'BIS_GIGACHAT_API_KEY'}
    if body.provider in ('openai', 'anthropic', 'gigachat', 'yandex') and not body.api_key:
        existing_key = os.environ.get('YANDEX_API_KEY') if body.provider == 'yandex' else os.environ.get(key_env[body.provider])
        if not existing_key:
            raise HTTPException(422, 'Введите API-ключ выбранного провайдера')
    if body.api_key and any(c.isspace() for c in body.api_key.get_secret_value().strip()):
        raise HTTPException(422, 'Ключ API содержит пробелы; проверьте его и повторите ввод')
    if base_url:
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise HTTPException(422, 'Адрес API должен начинаться с http:// или https://')

    provider_name = {'anthropic': 'anthropic-claude', 'openai': 'openai', 'ollama': 'ollama',
                     'compatible': 'custom-model', 'gigachat': 'gigachat', 'yandex': 'yandexgpt'}[body.provider]

    configured = get_settings()
    try:
        previous_providers, previous_routes = configured.llm_providers(), configured.llm_routes()
    except (ValueError, OSError):
        previous_providers, previous_routes = [], {}
    vision_providers = [p.model_dump(exclude_none=True) for p in previous_providers
                        if 'vision' in p.capabilities and p.kind != 'offline' and p.name != provider_name]
    if body.provider in ('anthropic', 'openai'):
        vision_providers = []  # no retired cloud providers in Claude checks or fallbacks
    vision_names = {p['name'] for p in vision_providers}
    vision_order = [n for n in previous_routes.get('vision', vision_names) if n in vision_names]

    selected = {'name': provider_name, 'kind': kind, 'model': model,
                'max_sensitivity': 'internal', 'mask_pii': body.provider in ('anthropic', 'openai', 'yandex'),
                'capabilities': ['text', 'tools', *(['vision'] if body.provider in ('anthropic', 'openai') else [])],
                'timeout': 60 if body.provider in ('anthropic', 'openai') else 120, 'temperature': 0.2}
    if base_url:
        selected['base_url'] = base_url.rstrip('/')
    if body.provider == 'yandex':
        selected.update(api_key_env='YANDEX_API_KEY', auth_scheme='Api-Key')
    elif body.provider == 'ollama':
        selected['auth_scheme'] = 'Bearer'
    elif body.provider == 'gigachat':
        selected.update(api_key_env=key_env['gigachat'], extra={'scope': body.scope})
    else:
        selected['api_key_env'] = key_env[body.provider]

    config = root / 'config' / f'llm.pilot.{uuid.uuid4().hex}.json'
    config.parent.mkdir(parents=True, exist_ok=True)
    vision_route = [provider_name, 'offline'] if body.provider in ('anthropic', 'openai') else [
        provider_name, *(n for n in vision_order if n != provider_name), 'offline']
    config.write_text(json.dumps({'providers': [selected, *vision_providers,
        {'name': 'offline', 'kind': 'offline', 'max_sensitivity': 'confidential'}],
        'routes': {'default': [provider_name, 'offline'], 'vision': vision_route}},
        ensure_ascii=False, indent=2), encoding='utf-8')
    values = {'BIS_LLM_CONFIG_FILE': config.as_posix()}
    if body.provider == 'yandex':
        values.update(YANDEX_FOLDER_ID=body.folder_id, BIS_YANDEX_FOLDER_ID=body.folder_id)
        if body.api_key:
            values['YANDEX_API_KEY'] = body.api_key.get_secret_value().strip()
        values['BIS_OCR_PROVIDER'] = 'yandex'
    else:
        if body.provider != 'ollama' and body.api_key:
            values[key_env[body.provider]] = body.api_key.get_secret_value().strip()
        if body.provider in ('anthropic', 'openai'):
            values['BIS_OCR_PROVIDER'] = body.provider
            if configured.embedding_provider == 'yandex':
                values.update(BIS_EMBEDDING_PROVIDER='hash', BIS_VECTOR_ENABLED='false')
    save(values)
    set_gateway(None)
    from bis.brain.vision import reset_status
    from bis.rag.embeddings import set_embedder
    from bis.rag.search import invalidate_vectors

    reset_status()
    set_embedder(None)
    invalidate_vectors()
    return {'saved': True, 'provider': provider_name, 'label': label, 'configuration': _reading_status(),
            'message': f'{label} выбран для языковой модели. OCR и голосовые настройки сохранены отдельно.'}


@router.post('/ai-setup')
def ai_setup(body: AISetup, user: User = Depends(require('settings:write'))):
    from bis.services.local_config import LOCK

    with LOCK:
        # Old browser bundles submit this Yandex-only endpoint. Never send a Claude key there.
        if body.api_key and body.api_key.get_secret_value().strip().startswith('sk-ant-'):
            return _save_ai_provider(AIProviderSetup(provider='anthropic', api_key=body.api_key))
        if body.api_key and body.api_key.get_secret_value().strip().startswith(('sk-proj-', 'sk-svcacct-')):
            return _save_ai_provider(AIProviderSetup(provider='openai', api_key=body.api_key))
        return _save_ai_setup(body)


def _save_ai_setup(body: AISetup):
    """Publish an immutable model config before switching the saved environment to it."""
    import json
    import os
    import uuid
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
    key = body.api_key.get_secret_value().strip() if body.api_key else (os.environ.get('YANDEX_API_KEY') or get_settings().yandex()[1])
    if not key or any(c.isspace() for c in key):
        raise HTTPException(422, 'Введите действующий API-ключ без пробелов')
    config = root / 'config' / f'llm.pilot.{uuid.uuid4().hex}.json'
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
              'BIS_STT_PROVIDER': 'yandex' if body.speech else 'openai', 'BIS_YANDEX_API_KEY_ENV': 'YANDEX_API_KEY'}
    from bis.services.local_config import save

    save(values)
    set_gateway(None)
    return {'saved': True, 'requires_restart': False,
            'message': 'Настройки сохранены. Проверьте ответ модели; OCR и голос проверяются на файле и записи.'}


@router.post('/ocr-check')
def check_ocr(db: Session = Depends(get_db), user: User = Depends(require('settings:write'))):
    db.close()  # release the authentication connection before a slow cloud call
    import io

    from PIL import Image, ImageDraw

    from bis import ocr

    picture = Image.new('RGB', (900, 180), 'white')
    ImageDraw.Draw(picture).text((30, 45), 'BIS OCR 12345', fill='black', font_size=60)
    image = io.BytesIO()
    picture.save(image, format='PNG')
    try:
        text = ocr.recognize(image.getvalue(), 'image/png')
    except ocr.OCRError as exc:
        raise HTTPException(502, str(exc)) from exc
    if '12345' not in text:
        raise HTTPException(502, 'OCR не прочитал контрольные цифры 12345. Проверьте провайдера распознавания.')
    return {'text': text}
