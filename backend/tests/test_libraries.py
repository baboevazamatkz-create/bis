"""Folder lifecycle, immutable forms and isolation before retrieval/generation."""
import datetime as dt
import io
import json
from zipfile import ZipFile

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


def test_letter_uses_confirmed_organization_original(db, demo, library):
    connection, root = library
    connection.level, connection.organization_id = 'organization', demo.id
    word = Word()
    word.sections[0].header.paragraphs[0].text = 'Контрольный фирменный бланк'
    word.add_paragraph('Текст письма:')
    buffer = io.BytesIO()
    word.save(buffer)
    path = root / 'Форма письма.docx'
    path.write_bytes(buffer.getvalue())
    synchronize(db, connection)
    version = db.scalar(select(LibraryVersion))
    mapping = forms.prepare(buffer.getvalue(), path.name, None)
    version.form_kind, version.mapping, version.approved_at = 'letter', mapping.model_dump(), dt.datetime.now(dt.UTC)
    db.flush()
    bind_context(db, demo.id)
    output = forms.render(db, get_storage(), demo.id, 'letter', {'body': 'Контрольный текст'},
                          organization_only=True)
    actual = Word(io.BytesIO(output))
    assert actual.sections[0].header.paragraphs[0].text == 'Контрольный фирменный бланк'
    assert any('Контрольный текст' in p.text for p in actual.paragraphs)
    assert db.info['used_library_form']['version_id'] == str(version.id)
    assert path.read_bytes() == buffer.getvalue()


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
    body['preview_token'] = preview.headers['X-BIS-Form-Preview']
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


def _aosr_training_doc(*, ambiguous=False):
    from docx.shared import Mm

    doc = Word()
    section = doc.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    doc.add_heading("Учебный акт освидетельствования скрытых работ", 0)
    section.header.paragraphs[0].text = "Учебный образец"
    table = doc.add_table(rows=0, cols=1)
    table.style = "Table Grid"
    fields = ("object_name", "developer_text", "builder_text", "number", "date", "representatives", "works",
              "project_docs", "materials", "executive_docs", "work_start", "work_end", "norms")
    canonical = {"object_name": "Объект капитального строительства", "developer_text": "Застройщик или технический заказчик"}
    if ambiguous:
        canonical["works"] = "Описание результата на участке"
    for field in fields:
        p = table.add_row().cells[0].paragraphs[0]
        p.style = "Normal"
        label = canonical.get(field, forms.FIELDS[field])
        p.add_run(label + ": ").bold = True
        p.add_run("_____").italic = True
    result = io.BytesIO()
    doc.save(result)
    return result.getvalue()


def test_aosr_known_slots_skip_models_and_preserve_existing_styles():
    original = _aosr_training_doc()

    def unnecessary_model():
        raise AssertionError("Known fields should not initialize a model")

    mapping, origins, _ = forms.prepare_aosr(original, "Учебный.docx", unnecessary_model)
    assert {e.field for e in mapping.entries} == forms.REQUIRED["aosr"]
    assert set(origins.values()) == {"rule"}
    preview = forms.preview_aosr(original, "Учебный.docx", mapping)
    with ZipFile(io.BytesIO(original)) as source, ZipFile(io.BytesIO(preview)) as result:
        assert set(source.namelist()) == set(result.namelist())
        assert [name for name in source.namelist() if source.read(name) != result.read(name)] == ["word/document.xml"]
    before, after = Word(io.BytesIO(original)), Word(io.BytesIO(preview))
    assert len(after.tables[0].rows) == len(before.tables[0].rows)
    assert after.tables[0].style.name == before.tables[0].style.name == "Table Grid"
    assert after.sections[0].header.paragraphs[0].text == "Учебный образец"
    for old_row, new_row in zip(before.tables[0].rows, after.tables[0].rows, strict=True):
        old, new = old_row.cells[0].paragraphs[0], new_row.cells[0].paragraphs[0]
        assert new.style.name == old.style.name
        assert new.runs[0].text == old.runs[0].text and new.runs[0].bold
        assert new.runs[1].italic and new.runs[1].text.startswith("ПРИМЕР: ")
        assert "_" not in new.text


def test_aosr_only_accepts_model_pairs_from_unresolved_slots():
    from bis.llm.gateway import LLMGateway, ProviderEntry
    from bis.llm.providers.offline import ScriptedProvider

    data = _aosr_training_doc(ambiguous=True)
    _, places = forms.targets(data, "Учебный.docx")
    unresolved = next(key for key, value in places.items() if value.startswith("Описание результата"))
    protected = next(key for key, value in places.items() if value.startswith("Номер:"))
    provider = ScriptedProvider(
        ['{"entries":[{"field":"works","target":"' + unresolved + '"},'
         '{"field":"materials","target":"' + protected + '"},{"field":"body","target":"99999"}]}'],
        name="language-test",
    )
    vision_only = ScriptedProvider(name="vision")
    gateway = LLMGateway(entries={
        "vision": ProviderEntry(vision_only, capabilities=("vision",)),
        "language-test": ProviderEntry(provider, "internal"),
    }, routes={"executive_document": ["vision", "language-test"]})
    mapping, origins, _ = forms.prepare_aosr(data, "Учебный.docx", lambda: gateway)
    assert origins[unresolved] == "model" and origins[protected] == "rule"
    assert {entry.target: entry.field for entry in mapping.entries}[protected] == "number"
    assert all(entry.field != "body" for entry in mapping.entries)
    assert not vision_only.calls and len(provider.calls) == 1
    prompt = provider.calls[0]["messages"][-1].content
    assert "Описание результата" in prompt and "Номер:" not in prompt and "Учебный образец" not in prompt


def test_docx_aosr_api_reviews_manual_correction_and_reuses_version(client, auth, db, demo, tmp_path, monkeypatch):
    from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
    from bis.llm.providers.offline import ScriptedProvider

    monkeypatch.setattr(get_settings(), "library_roots", [tmp_path])
    original = _aosr_training_doc(ambiguous=True)
    filename = "Учебный АОСР.docx"
    response = client.post('/api/libraries/upload', headers=auth, data={"level": "organization"},
                           files={"files": (filename, original)})
    assert response.status_code == 201, response.text
    connection_id = response.json()["id"]
    rows = client.get(f"/api/libraries/{connection_id}/files", headers=auth).json()
    version_id = rows[0]["active_version_id"]
    path = f"/api/libraries/versions/{version_id}"
    set_gateway(LLMGateway(entries={}))
    response = client.post(path + "/prepare", headers=auth, params={"kind": "aosr"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "works" not in {item["field"] for item in payload["entries"]}
    target = next(key for key, label in payload["targets"].items() if label.startswith("Описание результата"))
    mapping = {"format": payload["format"], "entries": payload["entries"]}
    incomplete = client.post(path + "/preview", headers=auth, json={"kind": "aosr", "mapping": mapping})
    assert incomplete.status_code == 400 and "Выполненные работы" in incomplete.json()["detail"]

    provider = ScriptedProvider(['{"entries":[{"field":"next_works","target":"' + target + '"}]}'])
    set_gateway(LLMGateway(entries={"model": ProviderEntry(provider)}))
    response = client.post(path + "/prepare", headers=auth, params={"kind": "aosr"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["origins"][target] == "model"
    suggestion = next(entry for entry in payload["entries"] if entry["target"] == target)
    assert suggestion["field"] == "next_works"
    suggestion["field"] = "works"
    mapping["entries"] = payload["entries"]

    preview = client.post(path + "/preview", headers=auth, json={"kind": "aosr", "mapping": mapping})
    assert preview.status_code == 200, preview.text[:300]
    assert "ПРИМЕР: Выполненные работы" in forms.paragraphs(Word(io.BytesIO(preview.content)))[int(target)].text
    approval = {"kind": "aosr", "mapping": mapping}
    assert client.post(path + "/approve", headers=auth, json=approval).status_code == 400
    approval["preview_token"] = preview.headers["X-BIS-Form-Preview"]
    changed = {"format": mapping["format"], "entries": [*mapping["entries"], {
        "field": "next_works", "target": "0", "anchor": payload["targets"]["0"], "mode": "append",
    }]}
    assert client.post(path + "/approve", headers=auth, json={**approval, "mapping": changed}).status_code == 400
    prepare_generic = forms.prepare

    def approve_during_prepare(raw, name, gateway):
        response = client.post(path + "/approve", headers=auth, json=approval)
        assert response.status_code == 200, response.text
        return prepare_generic(raw, name, gateway)

    with monkeypatch.context() as scoped:
        scoped.setattr(forms, "prepare", approve_during_prepare)
        assert client.post(path + "/prepare", headers=auth).status_code == 409

    def disabled_model():
        raise AssertionError("An approved mapping should not use a model")

    monkeypatch.setattr("bis.llm.gateway.get_gateway", disabled_model)
    monkeypatch.setattr("bis.api.deps.get_gateway", disabled_model)
    db.expire_all()
    project = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    response = client.post("/api/documents/aosr", headers=auth, json={
        "object_id": str(project.id), "works": "Монтаж кабельного лотка", "project_docs": "РД-01, лист 4",
        "work_start": "2026-11-12", "work_end": "2026-11-13",
        "materials": ["Лоток Л-1, паспорт 4"], "executive_docs": ["Исполнительная схема 1"], "norms": ["СП 76.13330"],
    })
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["data"]["library_form"]["version_id"] == version_id
    output = client.get(f'/api/documents/{document["id"]}/file', headers=auth)
    assert output.status_code == 200
    assert "Монтаж кабельного лотка" in forms.paragraphs(Word(io.BytesIO(output.content)))[int(target)].text
    version = db.scalar(select(LibraryVersion).where(LibraryVersion.approved_at.is_not(None)))
    assert get_storage().get(version.file_key) == original
    assert {key: version.mapping[key] for key in ("format", "entries", "profile")} == {**mapping, "profile": "aosr_docx"}

    source = next(tmp_path.glob(f"uploads/*/*/{filename}"))
    source.write_bytes(_aosr_training_doc())
    response = client.post(f"/api/libraries/{connection_id}/sync", headers=auth)
    assert response.status_code == 202
    new_row = client.get(f"/api/libraries/{connection_id}/files", headers=auth).json()[0]
    assert new_row["active_version_id"] != version_id
    assert not next(v for v in new_row["versions"] if v["id"] == new_row["active_version_id"])["approved_at"]


def test_aosr_slot_refuses_to_rewrite_a_run_with_a_page_break():
    from docx.enum.text import WD_BREAK

    original = _aosr_training_doc()
    document = Word(io.BytesIO(original))
    paragraph = document.tables[0].rows[0].cells[0].paragraphs[0]
    paragraph.runs[1].add_break(WD_BREAK.PAGE)
    data = io.BytesIO()
    document.save(data)
    mapping, _, _ = forms.prepare_aosr(data.getvalue(), "Учебный.docx", None)
    with pytest.raises(ValueError, match="нетекстовым объектом"):
        forms.preview_aosr(data.getvalue(), "Учебный.docx", mapping)


def _filled_aosr_example():
    doc = Word()
    header = doc.add_paragraph()
    header.add_run("Акт освидетельствования скрытых работ № ").bold = True
    header.add_run("ОБРАЗЕЦ-99").italic = True
    header.add_run(" от ").bold = True
    header.add_run("03.02.2024").underline = True
    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for field in sorted(forms.REQUIRED["aosr"] - {"number", "date", "works", "norms"}):
        left, right = table.add_row().cells
        left.paragraphs[0].add_run(forms.FIELDS[field]).bold = True
        right.paragraphs[0].add_run(f"СТАРОЕ-{field}").italic = True
    doc.add_paragraph("1. К освидетельствованию предъявлены следующие работы:\tСТАРОЕ-works")
    p = doc.add_paragraph("6. Работы выполнены в соответствии с ")
    p.add_run("СТАРОЕ-norms").underline = True
    doc.add_paragraph("Защищаемая подпись: СТАТИЧЕСКИЙ ТЕКСТ")
    result = io.BytesIO()
    doc.save(result)
    return result.getvalue()


def test_filled_aosr_example_replaces_values_and_preserves_labels_and_styles():
    original = _filled_aosr_example()
    mapping, _, _ = forms.prepare_aosr_example(original, "Готовый акт.docx")
    assert {entry.field for entry in mapping.entries} == forms.REQUIRED["aosr"]
    preview = forms.preview_aosr(original, "Готовый акт.docx", mapping)
    doc = Word(io.BytesIO(preview))
    assert doc.paragraphs[0].text == "Акт освидетельствования скрытых работ № ПРИМЕР: Номер от ПРИМЕР: Дата составления"
    assert doc.paragraphs[0].runs[0].bold and doc.paragraphs[0].runs[1].italic
    assert doc.paragraphs[0].runs[-1].underline
    assert doc.tables[0].style.name == "Table Grid"
    assert all(row.cells[0].paragraphs[0].runs[0].bold and row.cells[1].paragraphs[0].runs[0].italic
               for row in doc.tables[0].rows)
    text = "\n".join(p.text for p in forms.paragraphs(doc))
    assert text.count("ПРИМЕР: ") == len(forms.REQUIRED["aosr"])
    assert "СТАРОЕ-" not in text and "ОБРАЗЕЦ-99" not in text and "03.02.2024" not in text
    assert "работы:\tПРИМЕР: Выполненные работы" in text
    assert "Защищаемая подпись: СТАТИЧЕСКИЙ ТЕКСТ" in text

    first = mapping.entries[0]
    bad = first.model_copy(update={"target": first.target.rsplit(":", 1)[0] + ":99999"})
    with pytest.raises(ValueError):
        forms.validate_mapping(original, "Готовый акт.docx",
                               mapping.model_copy(update={"entries": [bad, *mapping.entries[1:]]}), "aosr")


def test_filled_aosr_example_can_map_representatives_individually():
    source = Word(io.BytesIO(_filled_aosr_example()))
    row = next(row for row in source.tables[0].rows if row.cells[0].text == forms.FIELDS["representatives"])
    for role, meta in forms.REPRESENTATIVE_ROLES.items():
        if meta["required"]:
            source.add_paragraph(meta["title"] + f": СТАРЫЙ-{role}")
    row._tr.getparent().remove(row._tr)
    original = io.BytesIO()
    source.save(original)
    mapping, _, _ = forms.prepare_aosr_example(original.getvalue(), "Готовый акт.docx")
    assert {entry.field for entry in mapping.entries} >= forms.REPRESENTATIVE_FIELDS
    preview = forms.preview_aosr(original.getvalue(), "Готовый акт.docx", mapping)
    assert "СТАРЫЙ-" not in "\n".join(p.text for p in forms.paragraphs(Word(io.BytesIO(preview))))


def test_filled_aosr_example_api_uses_approved_ranges(client, auth, db, demo, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "library_roots", [tmp_path])
    filename = "Заполненный АОСР.docx"
    original = _filled_aosr_example()
    upload = client.post("/api/libraries/upload", headers=auth, data={"level": "organization"},
                         files={"files": (filename, original)})
    assert upload.status_code == 201, upload.text
    connection_id = upload.json()["id"]
    version_id = client.get(f"/api/libraries/{connection_id}/files", headers=auth).json()[0]["active_version_id"]
    url = f"/api/libraries/versions/{version_id}"

    def unavailable_model():
        raise AssertionError("The example must be processed locally")

    monkeypatch.setattr("bis.llm.gateway.get_gateway", unavailable_model)
    prepared = client.post(url + "/prepare", headers=auth, params={"kind": "aosr", "example": "true"})
    assert prepared.status_code == 200, prepared.text
    review = prepared.json()
    assert review["example"] and len(review["entries"]) == len(forms.REQUIRED["aosr"])
    body = {"kind": "aosr", "mapping": {"format": review["format"], "entries": review["entries"]}}
    preview = client.post(url + "/preview", headers=auth, json=body)
    assert preview.status_code == 200, preview.text[:300]
    body["preview_token"] = preview.headers["X-BIS-Form-Preview"]
    response = client.post(url + "/approve", headers=auth, json=body)
    assert response.status_code == 200, response.text
    db.expire_all()
    project = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    generated = client.post("/api/documents/aosr", headers=auth, json={
        "object_id": str(project.id), "works": "Новая прокладка кабеля", "project_docs": "РД-02",
        "work_start": "2026-11-12", "work_end": "2026-11-13",
    })
    assert generated.status_code == 201, generated.text
    doc = generated.json()
    assert doc["data"]["library_form"]["version_id"] == version_id
    file = client.get(f'/api/documents/{doc["id"]}/file', headers=auth)
    assert file.status_code == 200
    document = Word(io.BytesIO(file.content))
    text = "\n".join(p.text for p in forms.paragraphs(document))
    assert "Новая прокладка кабеля" in text
    assert "СТАРОЕ-" not in text and "ОБРАЗЕЦ-99" not in text and "03.02.2024" not in text
    assert document.sections[0].header.paragraphs[0].text == ""
    assert client.get(url + "/file", headers=auth).content == original


def test_filled_example_recognizes_stock_aosr_and_removes_old_signatory_names():
    from bis.documents.aosr import AOSRData, Representative, render_aosr
    from bis.documents.common import Party

    data = AOSRData(
        number="АОСР-99", date=dt.date(2024, 2, 3), object_name="Прежний объект",
        developer=Party(name="Прежний заказчик"), builder=Party(name="Прежний подрядчик"),
        works="Прежние работы", project_docs="Старый шифр", materials=["Старый материал"],
        executive_docs=["Старая схема"], work_start=dt.date(2024, 1, 31), work_end=dt.date(2024, 2, 1),
        norms=["Старый СП"], copies=22, attachments=["Старый лист", "Старая схема приложения"],
        representatives={role: Representative(position="Инженер", full_name="Прежний Иван Иванович")
                                              for role in forms.REPRESENTATIVE_ROLES},
    )
    original = render_aosr(data)
    mapping, _, _ = forms.prepare_aosr_example(original, "Готовый акт.docx")
    preview = forms.preview_aosr(original, "Готовый акт.docx", mapping)
    text = "\n".join(p.text for p in forms.paragraphs(Word(io.BytesIO(preview))))
    assert "Прежн" not in text and "Стары" not in text
    assert "_______________ (подпись)" in text
    assert "Старая схема приложения" not in text and "22 экземплярах" not in text


def test_filled_example_does_not_guess_bare_number_or_adjacent_labeled_value():
    doc = Word()
    doc.add_paragraph("№ П-028")
    left, middle, right = doc.add_table(rows=1, cols=3).rows[0].cells
    left.text = "Материалы"
    middle.text = "Начало работ: 01.02.2024"
    right.text = "Лоток старого акта"
    source = io.BytesIO()
    doc.save(source)
    mapping, _, _ = forms.prepare_aosr_example(source.getvalue(), "Готовый акт.docx")
    assert {entry.field for entry in mapping.entries} == {"work_start"}


@pytest.mark.parametrize("content_control", [False, True])
def test_filled_example_rejects_hidden_text(content_control):
    from docx.oxml import OxmlElement

    doc = Word()
    if content_control:
        doc.element.body.insert(0, OxmlElement("w:sdt"))
    else:
        doc.add_paragraph().add_run("Скрытые реквизиты").font.hidden = True
    source = io.BytesIO()
    doc.save(source)
    with pytest.raises(ValueError, match="скрытый текст"):
        forms.prepare_aosr_example(source.getvalue(), "Готовый акт.docx")


def _aosr_precision_doc(scenario, *, ambiguous=False):
    from docx.shared import Mm, Pt

    doc = Word()
    doc.sections[0].page_width, doc.sections[0].page_height = Mm(210), Mm(297)
    doc.add_heading("Акт освидетельствования скрытых работ", 0)
    doc.add_heading("Сроки работ" if scenario == "inline" else "Участники строительства", 2)
    if scenario == "inline":
        p = doc.add_paragraph()
        for label in ("Начало работ", "Окончание работ"):
            p.add_run(label + ": ").bold = True
            p.add_run("___").italic = True
            p.add_run("___").italic = True
            if label == "Начало работ":
                p.add_run("\t")
        skipped = {"work_start", "work_end"}
        if ambiguous:
            doc.add_paragraph("Рабочая документация: ______    Основание проверки: ______")
            skipped.update(("project_docs", "executive_docs"))
    else:
        table = doc.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        label, value = table.rows[0].cells
        label.paragraphs[0].add_run(forms.FIELDS["builder_text"]).bold = True
        style = value.paragraphs[0].add_run()
        style.italic, style.font.size = True, Pt(9)
        skipped = {"builder_text"}
        if ambiguous:
            table.add_row().cells[0].text = "Сведения об использованном"
            skipped.add("materials")
    doc.add_paragraph("Контрольный текст вне подписей:")
    for field in sorted(forms.REQUIRED["aosr"] - skipped):
        doc.add_paragraph(forms.FIELDS[field] + ": ______")
    result = io.BytesIO()
    doc.save(result)
    return result.getvalue()


def test_aosr_blank_supports_two_independently_addressable_dates_in_one_paragraph():
    source = _aosr_precision_doc("inline")
    mapping, origins, _ = forms.prepare_aosr(source, "Сроки.docx", None)
    dates = {entry.field: entry for entry in mapping.entries if entry.field in {"work_start", "work_end"}}
    assert dates.keys() == {"work_start", "work_end"}
    assert dates["work_start"].target != dates["work_end"].target
    assert all(origins[entry.target] == "rule" for entry in dates.values())
    forms.validate_mapping(source, "Сроки.docx", mapping, "aosr")
    preview = Word(io.BytesIO(forms.preview_aosr(source, "Сроки.docx", mapping)))
    assert preview.paragraphs[2].text == "Начало работ: 12.01.2026\tОкончание работ: 15.01.2026"
    output = forms.filled(source, "Сроки.docx", mapping, {
        "work_start": dt.date(2026, 11, 12), "work_end": dt.date(2026, 11, 15),
    })
    p = Word(io.BytesIO(output)).paragraphs[2]
    assert p.text == "Начало работ: 12.11.2026\tОкончание работ: 15.11.2026"
    assert p.runs[0].bold and p.runs[1].italic and p.runs[4].bold and p.runs[5].italic


def test_aosr_blank_fills_empty_right_cell_without_touching_left_label():
    source = _aosr_precision_doc("table")
    mapping, _, _ = forms.prepare_aosr(source, "Таблица.docx", None)
    forms.validate_mapping(source, "Таблица.docx", mapping, "aosr")
    output = forms.filled(source, "Таблица.docx", mapping, {"builder_text": "ООО Учебный подрядчик"})
    before = Word(io.BytesIO(source)).tables[0]
    after = Word(io.BytesIO(output)).tables[0]
    assert after.style.name == before.style.name == "Table Grid"
    assert len(after.rows) == 1 and len(after.columns) == 2
    assert after.cell(0, 0)._tc.xml == before.cell(0, 0)._tc.xml
    assert after.cell(0, 1).text == "ООО Учебный подрядчик"
    assert after.cell(0, 1).paragraphs[0].runs[-1].italic
    assert after.cell(0, 1).paragraphs[0].runs[-1].font.size.pt == 9


def test_aosr_model_receives_only_local_context_for_unresolved_span():
    from bis.llm.gateway import LLMGateway, ProviderEntry
    from bis.llm.providers.offline import ScriptedProvider

    original = _aosr_precision_doc("inline", ambiguous=True)
    labels = {}
    manual, _, places = forms.prepare_aosr(original, "Сроки.docx", lambda: LLMGateway(entries={}), target_labels=labels)
    unresolved = next(target for target, label in labels.items() if label == "Основание проверки")
    assert unresolved in places and unresolved not in {entry.target for entry in manual.entries}
    neighbour = next(entry.target for entry in manual.entries if entry.field == "project_docs")
    reply = json.dumps({"entries": [
        {"field": "executive_docs", "target": unresolved},
        {"field": "next_works", "target": neighbour},
        {"field": "materials", "target": "999"},
    ]})
    provider = ScriptedProvider([reply], name="context-test")
    gateway = LLMGateway(entries={"context-test": ProviderEntry(provider)})
    mapping, origins, _ = forms.prepare_aosr(original, "Сроки.docx", lambda: gateway)
    assert next(entry for entry in mapping.entries if entry.target == unresolved).field == "executive_docs"
    assert origins[unresolved] == "model" and origins[neighbour] == "rule"
    assert "999" not in origins
    prompt = provider.calls[0]["messages"][-1].content
    assert "Сроки работ" in prompt and "Рабочая документация" in prompt and "Основание проверки" in prompt
    assert "Контрольный текст вне подписей" not in prompt and "______" not in prompt


def test_aosr_legacy_approved_mapping_reuses_numeric_paragraph_address():
    original = _aosr_training_doc()
    _, places = forms.targets(original, "Старый.docx", existing_parts_only=True)
    saved = {
        "format": "docx", "profile": "aosr_docx",
        "entries": [{"field": field, "target": target, "anchor": anchor, "mode": "slot"}
                    for field in forms.REQUIRED["aosr"] for target, anchor in places.items()
                    if anchor.startswith(forms.FIELDS[field] + ":") or
                    field == "object_name" and anchor.startswith("Объект капитального строительства") or
                    field == "developer_text" and anchor.startswith("Застройщик или технический заказчик")],
    }
    mapping = forms.Mapping.model_validate(saved)
    forms.validate_mapping(original, "Старый.docx", mapping, "aosr")
    content = forms.filled(original, "Старый.docx", mapping, {"works": "Заполнение по прежней карте"})
    doc = Word(io.BytesIO(content))
    assert any(p.text == "Выполненные работы: Заполнение по прежней карте" for p in forms.paragraphs(doc))


def test_aosr_slot_span_cannot_overwrite_label_or_overlap_numeric_address():
    original = _aosr_precision_doc("inline")
    mapping, _, _ = forms.prepare_aosr(original, "Сроки.docx", None)
    entry = next(entry for entry in mapping.entries if entry.field == "work_start")
    paragraph, start, end = forms.span_address(entry.target)
    corrupted = entry.model_copy(update={"target": f"{paragraph}:{start - 1}:{end}"})
    entries = [corrupted if item.target == entry.target else item for item in mapping.entries]
    with pytest.raises(ValueError, match="не совпадает с заполнителем"):
        forms.validate_mapping(original, "Сроки.docx", mapping.model_copy(update={"entries": entries}), "aosr")
    overlapping = entry.model_copy(update={"target": str(paragraph), "mode": "replace"})
    with pytest.raises(ValueError, match="пересекаются"):
        forms.validate_mapping(original, "Сроки.docx", mapping.model_copy(update={"entries": [*mapping.entries, overlapping]}), "aosr")


def test_aosr_two_slots_in_same_run_allow_multiline_value_on_right():
    doc = Word()
    doc.add_paragraph("Рабочая документация: ______   Материалы: ______").runs[0].bold = True
    source = io.BytesIO()
    doc.save(source)
    original = source.getvalue()
    mapping, _, _ = forms.prepare_aosr(original, "Бланк.docx", None)
    output = forms.filled(original, "Бланк.docx", mapping, {
        "project_docs": "РД-21", "materials": ["Кабель", "Лоток"],
    })
    p = Word(io.BytesIO(output)).paragraphs[0]
    assert p.text == "Рабочая документация: РД-21   Материалы: Кабель\nЛоток"
    assert p.runs[0].bold
