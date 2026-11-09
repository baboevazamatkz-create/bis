"""Folder lifecycle, immutable forms and isolation before retrieval/generation."""
import datetime as dt
import io

import pytest
from docx import Document as Word
from sqlalchemy import select

from bis.config import get_settings
from bis.models import ConstructionObject, Document, KnowledgeSource, User
from bis.models.libraries import LibraryConnection, LibraryFile, LibraryVersion
from bis.rag.embeddings import get_embedder
from bis.rag.search import hybrid_search
from bis.services import library_forms as forms
from bis.services.library_access import bind_context
from bis.services.library_sync import scan
from bis.services.storage import get_storage


@pytest.fixture
def library(db, demo, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), 'library_roots', [tmp_path])
    root = tmp_path / 'knowledge'
    root.mkdir()
    c = LibraryConnection(name='Нормативы', level='knowledge', path=str(root))
    db.add(c)
    db.flush()
    return c, root


def synchronize(db, c):
    return scan(db, get_storage(), get_embedder(), None, c)


def strict(db, org, obj=None):
    org.settings = {**(org.settings or {}), 'libraries_strict': True}
    db.flush()
    return bind_context(db, org.id, obj.id if obj else None)


def test_incremental_versions_removal_and_unavailable_folder(db, demo, library):
    c, root = library
    f = root / 'СП 123.txt'
    f.write_text('СП 123. Пункт 1. Изоляция должна быть проверена.', encoding='utf-8')
    assert synchronize(db, c)['updated'] == 1
    item = db.scalar(select(LibraryFile).where(LibraryFile.connection_id == c.id))
    first = item.active_version_id
    assert synchronize(db, c)['unchanged'] == 1
    f.write_text('СП 123. Пункт 1. Новая изоляция проверяется повторно.', encoding='utf-8')
    assert synchronize(db, c)['updated'] == 1
    assert item.active_version_id != first
    assert db.get(KnowledgeSource, db.get(LibraryVersion, first).source_id).status == 'replaced'
    c.path = str(root / 'missing')
    with pytest.raises(FileNotFoundError):
        synchronize(db, c)
    assert item.state == 'ready'
    c.path = str(root)
    f.unlink()
    assert synchronize(db, c)['removed'] == 1
    assert item.state == 'removed'


def test_strict_rag_excludes_demo_and_other_projects_and_unassigned(db, demo, library):
    c, root = library
    (root / 'СП 123.txt').write_text('СП 123. Изоляция из общей библиотеки.', encoding='utf-8')
    synchronize(db, c)
    from test_executive_pipeline import source

    from bis.api.deps import agent_context
    user = db.scalar(select(User).where(User.organization_id == demo.id))
    ctx = agent_context(db, user)
    doc = source(ctx, 'project_doc', 'Изоляция проекта A', 'Изоляция только проекта A.')
    other = ConstructionObject(organization_id=demo.id, name='Проект B')
    db.add(other)
    db.flush()
    alien = source(ctx, 'project_doc', 'Изоляция проекта B', 'Изоляция чужого проекта B.')
    alien.object_id = other.id
    loose = source(ctx, 'certificate', 'Изоляция не распределена', 'Изоляция неизвестного объекта.', location_object=False)
    db.flush()
    obj = db.get(ConstructionObject, doc.object_id)
    strict(db, demo, obj)
    hits = hybrid_search(db, get_embedder(), 'изоляция', org_id=demo.id, k=30)
    assert any(h.title == 'СП 123' for h in hits)
    assert any(h.document_id == str(doc.id) for h in hits)
    assert not any(h.document_id in (str(alien.id), str(loose.id)) for h in hits)
    assert db.scalar(select(Document.id).where(Document.id == alien.id)) is None
    bind_context(db, demo.id, other.id)
    hits = hybrid_search(db, get_embedder(), 'изоляция', org_id=demo.id, k=30)
    assert any(h.document_id == str(alien.id) for h in hits)
    assert not any(h.document_id == str(doc.id) for h in hits)


def test_paths_symlinks_and_unsupported_files(db, demo, library, tmp_path):
    c, root = library
    (root / 'old.doc').write_bytes(b'legacy')
    outside = tmp_path / 'outside.txt'
    outside.write_text('Не индексировать ссылку', encoding='utf-8')
    (root / 'link.txt').symlink_to(outside)
    result = synchronize(db, c)
    assert result['errors'] == 1
    files = db.scalars(select(LibraryFile).where(LibraryFile.connection_id == c.id)).all()
    assert [f.relative_path for f in files] == ['old.doc']
    assert 'DOCX' in files[0].error


def word_bytes():
    d = Word()
    for title in [forms.FIELDS[k] for k in sorted(forms.REQUIRED['aosr'])]:
        d.add_paragraph(title + ':')
    output = io.BytesIO()
    d.save(output)
    return output.getvalue()


def test_ordinary_form_requires_review_and_original_is_unchanged(db, demo, library):
    c, root = library
    original = word_bytes()
    (root / 'АОСР.docx').write_bytes(original)
    synchronize(db, c)
    version = db.scalar(select(LibraryVersion))
    mapping = forms.prepare(original, 'АОСР.docx', None)
    forms.validate_mapping(original, 'АОСР.docx', mapping, 'aosr')
    strict(db, demo)
    with pytest.raises(ValueError, match='Нет подтверждённой формы'):
        forms.select_form(db, demo.id, 'aosr')
    version.form_kind, version.mapping, version.approved_at = 'aosr', mapping.model_dump(), dt.datetime.now(dt.UTC)
    db.flush()
    content = forms.render(db, get_storage(), demo.id, 'aosr', {'works': 'Кабель участка 123', 'object_name': 'Объект А'})
    generated = Word(io.BytesIO(content))
    assert 'Кабель участка 123' in '\n'.join(p.text for p in generated.paragraphs)
    assert (root / 'АОСР.docx').read_bytes() == original
    assert db.info['used_library_form']['version_id'] == str(version.id)


def test_no_builtin_fallback_in_strict_mode(db, demo):
    from bis.documents.templates import render_with_org_template
    strict(db, demo)
    with pytest.raises(ValueError, match='Нет подтверждённой формы'):
        render_with_org_template(db, get_storage(), demo.id, 'aosr', {})


def test_connection_and_review_api_enforces_admin_and_paths(client, auth, db, demo, library):
    c, root = library
    db.commit()
    user = db.scalar(select(User).where(User.organization_id == demo.id, User.role == 'admin'))
    user.is_platform_admin = False
    db.commit()
    response = client.post('/api/libraries', headers=auth, json={'name': 'Denied', 'path': '/etc', 'level': 'organization'})
    assert response.status_code == 403
    response = client.post('/api/libraries', headers=auth, json={'name': 'Shared', 'path': str(root), 'level': 'knowledge'})
    assert response.status_code == 403
    (root / 'регламент.txt').write_text('Внутренний регламент организации.', encoding='utf-8')
    response = client.post('/api/libraries', headers=auth, json={'name': 'Организация', 'path': str(root), 'level': 'organization'})
    assert response.status_code == 201, response.text
    connection_id = response.json()['id']
    response = client.get(f'/api/libraries/{connection_id}/files', headers=auth)
    assert response.status_code == 200 and len(response.json()) == 1
    user.role = 'viewer'
    db.commit()
    assert client.put('/api/libraries/policy', headers=auth, json={'strict': True}).status_code == 403


def test_approval_preview_immutable_mapping_and_changed_file(client, auth, db, demo, library):
    c, root = library
    user = db.scalar(select(User).where(User.organization_id == demo.id, User.role == 'admin'))
    user.is_platform_admin = True
    (root / 'АОСР.docx').write_bytes(word_bytes())
    synchronize(db, c)
    version = db.scalar(select(LibraryVersion))
    db.commit()
    url = f'/api/libraries/versions/{version.id}'
    response = client.post(url + '/prepare', headers=auth)
    assert response.status_code == 200, response.text
    body = {'kind': 'aosr', 'mapping': response.json()}
    preview = client.post(url + '/preview', headers=auth, json=body)
    assert preview.status_code == 200 and preview.content.startswith(b'PK')
    response = client.post(url + '/approve', headers=auth, json=body)
    assert response.status_code == 200, response.text
    assert client.post(url + '/approve', headers=auth, json=body).status_code == 409
    assert client.post(url + '/prepare', headers=auth).status_code == 409
    (root / 'АОСР.docx').write_bytes(word_bytes() + b'changed')
    db.expire_all()
    synchronize(db, c)
    db.commit()
    assert client.post(url + '/approve', headers=auth, json=body).status_code == 409
    new = db.get(LibraryFile, version.file_id).active_version_id
    assert new != version.id and db.get(LibraryVersion, new).approved_at is None


def test_project_header_isolates_direct_document_access_and_chat(client, auth, db, demo):
    from test_executive_pipeline import source

    from bis.api.deps import agent_context
    user = db.scalar(select(User).where(User.organization_id == demo.id, User.role == 'admin'))
    ctx = agent_context(db, user)
    own = source(ctx, 'project_doc', 'Изоляция A', 'Проектная документация. Изоляция объекта A.')
    obj = db.get(ConstructionObject, own.object_id)
    other = ConstructionObject(organization_id=demo.id, name='Объект Б')
    db.add(other)
    db.flush()
    alien = source(ctx, 'project_doc', 'Изоляция B', 'Проектная документация. Изоляция объекта B.')
    alien.object_id = other.id
    strict(db, demo, obj)
    db.commit()
    ha = {**auth, 'X-BIS-Project': str(obj.id)}
    hb = {**auth, 'X-BIS-Project': str(other.id)}
    assert client.get(f'/api/documents/{own.id}/file', headers=ha).status_code == 200
    assert client.get(f'/api/documents/{alien.id}/file', headers=ha).status_code == 404
    first = client.post('/api/chat', headers=ha, json={'message': 'Какие нормативные требования по изоляции?', 'workspace': 'executive'})
    assert first.status_code == 200, first.text
    assert 'не найдено нормативного основания' in first.json()['text']
    session = first.json()['session_id']
    assert client.get(f'/api/chat/sessions/{session}?workspace=executive', headers=hb).status_code == 404
    assert client.post('/api/chat', headers=hb, json={'message': 'Продолжим', 'session_id': session, 'workspace': 'executive'}).status_code == 404
    assert client.get('/api/chat/sessions?workspace=executive', headers=hb).json() == []


def test_organization_base_is_shared_only_with_its_own_projects(db, demo, library):
    c, root = library
    c.level, c.organization_id = 'organization', demo.id
    (root / 'Регламент.txt').write_text('Изоляция: внутренний регламент компании.', encoding='utf-8')
    synchronize(db, c)
    other_org = __import__('bis.models', fromlist=['Organization']).Organization(name='Другая компания')
    db.add(other_org)
    db.flush()
    strict(db, demo)
    assert any('Регламент' in h.title for h in hybrid_search(db, get_embedder(), 'изоляция', org_id=demo.id))
    bind_context(db, other_org.id)
    strict(db, other_org)
    assert not hybrid_search(db, get_embedder(), 'изоляция', org_id=other_org.id)


def test_project_form_precedence_and_revoke(db, demo, library):
    c, root = library
    (root / 'АОСР.docx').write_bytes(word_bytes())
    synchronize(db, c)
    shared = db.scalar(select(LibraryVersion))
    shared.mapping, shared.form_kind, shared.approved_at = forms.prepare(word_bytes(), 'АОСР.docx', None).model_dump(), 'aosr', dt.datetime.now(dt.UTC)
    project = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    project_root = root.parent / 'project'
    project_root.mkdir()
    (project_root / 'АОСР.docx').write_bytes(word_bytes())
    cp = LibraryConnection(name='Проектная форма', level='project', organization_id=demo.id, object_id=project.id, path=str(project_root))
    db.add(cp)
    db.flush()
    synchronize(db, cp)
    item = db.scalar(select(LibraryFile).where(LibraryFile.connection_id == cp.id))
    specific = db.get(LibraryVersion, item.active_version_id)
    specific.mapping, specific.form_kind, specific.approved_at = shared.mapping, 'aosr', dt.datetime.now(dt.UTC)
    db.flush()
    strict(db, demo, project)
    assert forms.select_form(db, demo.id, 'aosr').id == specific.id
    specific.approved_at = None
    db.flush()
    assert forms.select_form(db, demo.id, 'aosr').id == shared.id


def test_excel_fills_reviewed_cells_preserves_formula_and_repeating_rows():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Журнал'
    ws['A1'] = 'Объект строительства:'
    ws['F10'] = '=SUM(F1:F9)'
    out = io.BytesIO()
    wb.save(out)
    mapping = forms.Mapping(format='xlsx', entries=[forms.Entry(field='rows', target='Журнал!A3', anchor='', mode='replace')])
    forms.validate_mapping(out.getvalue(), 'Журнал.xlsx', mapping, 'work_journal')
    content = forms.filled(out.getvalue(), 'Журнал.xlsx', mapping, {'rows': [['05.11.2026', 'Кабель'], ['06.11.2026', '=bad()']]})
    result = openpyxl.load_workbook(io.BytesIO(content))
    assert result['Журнал']['B3'].value == 'Кабель'
    assert result['Журнал']['B4'].value == "'=bad()"
    assert result['Журнал']['F10'].value == '=SUM(F1:F9)'


def test_pdf_coordinate_form_preserves_page_and_checks_overflow():
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(300, 400)
    raw = io.BytesIO()
    writer.write(raw)
    mapping = forms.Mapping(format='pdf_coordinates', entries=[forms.Entry(field='body', target='1:10:20:250:40', mode='replace')])
    forms.validate_mapping(raw.getvalue(), 'Бланк.pdf', mapping, 'letter')
    result = forms.filled(raw.getvalue(), 'Бланк.pdf', mapping, {'body': 'Проверка формы'})
    assert 'Проверка формы' in PdfReader(io.BytesIO(result)).pages[0].extract_text()
    with pytest.raises(ValueError, match='не помещается'):
        forms.filled(raw.getvalue(), 'Бланк.pdf', mapping, {'body': 'Очень длинный текст ' * 100})


@pytest.fixture
def executive_corpus(db, demo):
    from test_executive_pipeline import corpus

    from bis.api.deps import agent_context
    user = db.scalar(select(User).where(User.organization_id == demo.id, User.role == 'admin'))
    ctx = agent_context(db, user)
    ctx.gateway = None
    return ctx, corpus.__wrapped__(ctx)


def approved_library(db, library):
    c, root = library
    (root / 'АОСР.docx').write_bytes(word_bytes())
    (root / 'СП 76.13330.2016.txt').write_text('СП 76.13330.2016\n1. Электротехнические устройства. Прокладка кабеля по рабочей документации.', encoding='utf-8')
    synchronize(db, c)
    version = db.scalar(select(LibraryVersion).where(LibraryVersion.form_kind == 'aosr'))
    version.mapping, version.approved_at = forms.prepare(word_bytes(), 'АОСР.docx', None).model_dump(), dt.datetime.now(dt.UTC)
    norm = db.scalar(select(KnowledgeSource).where(KnowledgeSource.kind == 'norm', KnowledgeSource.title.contains('76.13330')))
    norm.status = 'active'
    db.flush()
    return version, norm


def test_strict_pipeline_preserves_plan_when_form_is_missing(db, demo, library, executive_corpus):
    from test_executive_pipeline import run
    ctx, corpus = executive_corpus
    strict(db, demo, db.get(ConstructionObject, corpus['project'].object_id))
    event = run(ctx)
    assert event.result['status'] == 'needs_data'
    assert event.document_id is None and event.result['documents'] == []
    assert event.result['plan']['work_end'] == '2026-11-05'
    assert any(g['field'] == 'form_version_id' for g in event.result['gaps'])


def test_strict_pipeline_uses_reviewed_form_and_versioned_norm_then_blocks_revocation(db, demo, library, executive_corpus):
    from test_executive_pipeline import run

    from bis.services.executive_pipeline import bundle, validate_sources
    ctx, corpus = executive_corpus
    version, norm = approved_library(db, library)
    strict(db, demo, db.get(ConstructionObject, corpus['project'].object_id))
    event = run(ctx)
    assert event.result['status'] == 'ready_for_review', event.result['gaps']
    doc = db.get(Document, event.document_id)
    assert doc.data['library_form']['version_id'] == str(version.id)
    assert any(e.get('source_id') == str(norm.id) and e.get('library_version_id') for e in doc.data['evidence'])
    text = '\n'.join(p.text for p in Word(io.BytesIO(get_storage().get(doc.file_key))).paragraphs)
    assert 'Проложен кабель' in text and '05.11.2026' in text and 'Сертификат С-1' in text
    assert bundle(ctx, event).startswith(b'PK')
    norm.status = 'cancelled'
    db.flush()
    with pytest.raises(ValueError, match='Нормативное основание'):
        validate_sources(db, get_storage(), doc)
    norm.status = 'active'
    version.approved_at = None
    db.flush()
    with pytest.raises(ValueError, match='Форма отозвана'):
        bundle(ctx, event)


def test_strict_pipeline_does_not_take_unassigned_certificate(db, demo, library, executive_corpus):
    from test_executive_pipeline import run
    ctx, corpus = executive_corpus
    approved_library(db, library)
    corpus['cert'].object_id = None
    db.flush()
    strict(db, demo, db.get(ConstructionObject, corpus['project'].object_id))
    event = run(ctx)
    assert any(g['field'].startswith('quality:') for g in event.result['gaps'])
    assert str(corpus['cert'].id) not in {a['id'] for a in event.result['attachments']}


def test_background_graph_resolution_does_not_link_another_project_or_change_context(db, demo, executive_corpus):
    from test_executive_pipeline import source

    from bis.brain.links import resolve
    ctx, corpus = executive_corpus
    other = ConstructionObject(organization_id=demo.id, name='Другой объект')
    db.add(other)
    db.flush()
    alien = source(ctx, 'certificate', 'Сертификат № С-99', 'Сертификат № С-99 только другого проекта.')
    alien.number, alien.object_id = 'С-99', other.id
    demo.settings = {**(demo.settings or {}), 'libraries_strict': True}
    db.flush()
    db.info.pop('source_context', None)
    assert resolve(db, demo.id, 'quality', 'С-99', object_id=corpus['project'].object_id) is None
    assert resolve(db, demo.id, 'quality', 'С-99', object_id=other.id).id == alien.id
    assert 'source_context' not in db.info


def test_unread_replacement_cannot_fall_back_to_previous_text(db, demo, library, monkeypatch):
    c, root = library
    f = root / 'СП 123.txt'
    f.write_text('Изоляция прежней редакции.', encoding='utf-8')
    synchronize(db, c)
    f.write_text('Изменённый, но непрочитанный файл.', encoding='utf-8')
    from bis.rag.parsers import Page
    from bis.services import archive
    monkeypatch.setattr(archive, '_pages', lambda *_: ([Page(1, '')], 'none'))
    assert synchronize(db, c)['errors'] == 1
    strict(db, demo)
    assert not hybrid_search(db, get_embedder(), 'изоляция', org_id=demo.id)


def test_organization_rule_adds_quality_requirement_to_executive_pipeline(db, demo, library, executive_corpus):
    from test_executive_pipeline import run
    ctx, corpus = executive_corpus
    approved_library(db, library)
    _, root = library
    rules = root.parent / 'organization'
    rules.mkdir()
    (rules / 'Регламент.txt').write_text('Требуется: Для прокладки кабеля обязательно приложить паспорт качества.', encoding='utf-8')
    c = LibraryConnection(name='Правила компании', level='organization', organization_id=demo.id, path=str(rules))
    db.add(c)
    db.flush()
    synchronize(db, c)
    strict(db, demo, db.get(ConstructionObject, corpus['project'].object_id))
    event = run(ctx)
    assert any(g['field'].startswith('quality:') and 'паспорт' in g['message'] for g in event.result['gaps'])
    assert any(e['field'] == 'quality_requirement' and e['method'] == 'library' for e in event.result['evidence'])


def test_project_memory_does_not_follow_user_to_another_project(client, auth, db, demo):
    project = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    other = ConstructionObject(organization_id=demo.id, name='Другой объект')
    db.add(other)
    db.flush()
    strict(db, demo, project)
    db.commit()
    headers = {**auth, 'X-BIS-Project': str(project.id)}
    result = client.post('/api/memory', headers=headers, json={'content': 'Правило только объекта А', 'kind': 'rule'})
    assert result.status_code == 201, result.text
    assert result.json()['object_id'] == str(project.id)
    result = client.get('/api/memory', headers={**auth, 'X-BIS-Project': str(other.id)})
    assert not any(m['content'] == 'Правило только объекта А' for m in result.json())


def test_strict_upload_uses_active_project_and_rejects_manual_norms(client, auth, db, demo):
    project = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    strict(db, demo, project)
    db.commit()
    headers = {**auth, 'X-BIS-Project': str(project.id)}
    response = client.post('/api/documents/upload', headers=headers,
        files={'file': ('РД.txt', 'Рабочая документация участка 123.'.encode(), 'text/plain')},
        data={'kind': 'project_doc', 'process': 'false'})
    assert response.status_code == 201, response.text
    assert response.json()['object_id'] == str(project.id)
    response = client.post('/api/knowledge/upload', headers=headers, files={'file': ('СП.txt', b'text', 'text/plain')})
    assert response.status_code == 409


def test_cli_grants_shared_library_administration_only_to_existing_admin(db, demo, capsys):
    from bis.cli import main
    user = db.scalar(select(User).where(User.organization_id == demo.id, User.role == 'admin'))
    user.is_platform_admin = False
    db.commit()
    assert main(['grant-platform-admin', '--email', user.email]) == 0
    db.refresh(user)
    assert user.is_platform_admin
    assert 'Управление общей библиотекой разрешено' in capsys.readouterr().out
    with pytest.raises(SystemExit, match='не найден'):
        main(['grant-platform-admin', '--email', 'nobody@example.invalid'])


def test_background_executive_api_keeps_selected_project_before_first_checkpoint(client, auth, db, demo, library, executive_corpus):
    ctx, corpus = executive_corpus
    approved_library(db, library)
    project = db.get(ConstructionObject, corpus['project'].object_id)
    strict(db, demo, project)
    db.commit()
    headers = {**auth, 'X-BIS-Project': str(project.id)}
    response = client.post('/api/executive/runs', headers=headers,
        json={'text': 'Проложен кабель на участке 123.', 'object_id': str(project.id)})
    assert response.status_code == 202, response.text
    result = client.get('/api/executive/runs/' + response.json()['id'], headers=headers).json()
    assert result['status'] == 'ready_for_review', result
    assert result['documents'] and result['plan']['work_end'] == '2026-11-05'
