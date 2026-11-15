"""Organization-independent authoring, real DOCX artifacts and continuity across turns."""
import io
import uuid
from types import SimpleNamespace
from zipfile import ZipFile

from docx import Document as Word

from bis.agents.base import AgentContext
from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
from bis.llm.providers.offline import ScriptedProvider
from bis.llm.types import Message, ToolCall
from bis.models import Document, Organization
from bis.rag.embeddings import get_embedder
from bis.services import document_authoring as authoring
from bis.services.storage import get_storage


def sample(db, org, kind='letter', brand=''):
    word = Word()
    word.styles['Normal'].font.name = 'Arial'
    word.sections[0].header.paragraphs[0].text = org.name + ' ' + brand
    word.add_paragraph('OLD RECIPIENT, OLD SECRET BODY, OLD SIGNER 01.01.2000 № 999')
    out = io.BytesIO()
    word.save(out)
    storage = get_storage()
    doc = Document(organization_id=org.id, kind=kind, title='Письмо образец' if kind == 'letter' else 'Приказ образец',
        source='uploaded', status='signed', file_name=f'example-{kind}.docx',
        file_key=storage.put(out.getvalue(), f'example-{kind}.docx', prefix='authoring-test'), data={'library_scope': 'organization'})
    db.add(doc)
    db.flush()
    return doc


def context(db, org):
    from bis.services.library_access import bind_context
    bind_context(db, org.id)
    return AgentContext(db=db, org_id=org.id, user=SimpleNamespace(id=uuid.uuid4(), role='admin', full_name='Инженер', position=None),
        storage=get_storage(), gateway=LLMGateway(entries={}), embedder=get_embedder(), agent_name='assistant')


def test_layout_tracks_organization_and_source_changes_without_old_content(db):
    first = Organization(name='ООО Организация Альфа', inn='1234567890', settings={'libraries_strict': True})
    second = Organization(name='ООО Организация Бета', inn='9876543210', settings={'libraries_strict': True})
    db.add_all([first, second])
    db.flush()
    a, b = sample(db, first, brand='ALPHA BRAND'), sample(db, second, brand='BETA BRAND')
    for org, expected, brand in ((first, a, 'ALPHA BRAND'), (second, b, 'BETA BRAND'), (first, a, 'ALPHA BRAND')):
        ctx = context(db, org)
        fresh = authoring.render_administrative(org, 'Приказ', 'NEW SUBJECT', ['NEW CONTENT'], '', '', {})
        content, basis = authoring.adapt_layout(db, ctx.storage, org.id, 'letter', fresh)
        assert basis['sample']['id'] == str(expected.id)
        with ZipFile(io.BytesIO(content)) as package:
            xml = '\n'.join(package.read(name).decode() for name in package.namelist() if name.endswith('.xml'))
        assert brand in xml and 'NEW CONTENT' in xml
        assert 'OLD RECIPIENT' not in xml and 'OLD SECRET BODY' not in xml and 'OLD SIGNER' not in xml
        assert ('BETA BRAND' if org is first else 'ALPHA BRAND') not in xml
    old_hash = basis['sha256']
    revised = Word()
    revised.sections[0].header.paragraphs[0].text = first.name + ' REVISED BRAND'
    buffer = io.BytesIO()
    revised.save(buffer)
    a.file_key = get_storage().put(buffer.getvalue(), 'revised.docx', prefix='authoring-test')
    db.flush()
    _, changed = authoring.adapt_layout(db, get_storage(), first.id, 'letter', fresh)
    assert changed['sha256'] != old_hash


def test_order_uses_same_discovery_and_creates_real_file(db, demo):
    from bis.agents.tools_authoring import DraftArgs, create_document_draft
    original = sample(db, demo, 'order', 'ORDER BRAND')
    ctx = context(db, demo)
    doc = create_document_draft.handler(ctx, DraftArgs(document_type='приказ', subject='О назначении работника',
        paragraphs=['Назначить Петрова ответственным за входной контроль.']))
    assert doc['created'] and doc['basis']['sample']['id'] == str(original.id)
    created = db.get(Document, uuid.UUID(doc['id']))
    assert created.file_key and created.kind == 'order' and created.status == 'draft'
    word = Word(io.BytesIO(ctx.storage.get(created.file_key)))
    assert 'Петрова' in '\n'.join(p.text for p in word.paragraphs)
    assert created.data['library_scope'] == 'organization'


def test_letter_in_strict_mode_adapts_existing_document_without_template_registration(client, auth, db, demo):
    from bis.agents.builtin_tools import LetterArgs, create_letter_draft
    demo.settings = {**(demo.settings or {}), 'libraries_strict': True}
    db.flush()
    original = sample(db, demo)
    ctx = context(db, demo)
    result = create_letter_draft.handler(ctx, LetterArgs(subject='О прекращении работ', body=['Уведомляем о прекращении работ.']))
    assert result['created'] and result['basis']['sample']['id'] == str(original.id)
    document = db.get(Document, uuid.UUID(result['id']))
    assert document.file_key and 'уточнить' in document.data['addressee'][0]
    db.commit()
    response = client.get(f'/api/documents/{document.id}/file', headers=auth)
    assert response.status_code == 200 and response.content.startswith(b'PK')


def test_false_created_claim_does_not_turn_into_a_file_and_yes_continues_task(client, auth, db):
    llm = ScriptedProvider(['Письмо подготовлено.'] * 3 + [Message(role='assistant', tool_calls=[
        ToolCall('letter', 'create_letter_draft', {'subject': 'О прекращении работ', 'body': ['Уведомляем о прекращении работ.']})]),
        'Черновик создан.'])
    set_gateway(LLMGateway(entries={'test': ProviderEntry(llm, 'confidential')}))
    first = client.post('/api/chat', headers=auth, json={'message': 'Подготовь письмо о прекращении работ по нашей форме', 'workspace': 'executive'})
    assert first.status_code == 200, first.text
    assert 'Файл пока не создан' in first.json()['text'] and not first.json()['documents']
    session = first.json()['session_id']
    # The failed first turn must retain the task; 'да' must not become a new contract search.
    second = client.post('/api/chat', headers=auth, json={'message': 'да', 'session_id': session, 'workspace': 'executive'})
    assert second.status_code == 200, second.text
    assert second.json()['documents'] and second.json()['documents'][0]['kind'] == 'letter'
    created_id = second.json()['documents'][0]['id']
    calls = len(llm.calls)
    third = client.post('/api/chat', headers=auth, json={'message': 'где файл письма?', 'session_id': session, 'workspace': 'executive'})
    assert third.status_code == 200 and third.json()['documents'][0]['id'] == created_id
    assert third.json()['provider'] == 'artifact_ledger' and len(llm.calls) == calls
    assert client.get(f'/api/documents/{created_id}/file', headers=auth).status_code == 200


def test_example_discovery_does_not_expand_project_access(db, demo):
    from bis.models import ConstructionObject
    from bis.services.library_access import bind_context
    own = sample(db, demo)
    other = ConstructionObject(organization_id=demo.id, name='Другой проект')
    db.add(other)
    db.flush()
    hidden = sample(db, demo)
    hidden.object_id = other.id
    hidden.title = 'Письмо другого проекта'
    db.flush()
    bind_context(db, demo.id)
    found = authoring.examples(db, demo.id, 'letter')
    assert str(own.id) in {item['id'] for item in found} and str(hidden.id) not in {item['id'] for item in found}


def test_full_document_search_prioritizes_rare_terms_over_boilerplate():
    from bis.services.document_text import query_windows
    chunks = [SimpleNamespace(content='Договор стороны работы общие условия ' * 100) for _ in range(70)]
    chunks.append(SimpleNamespace(content='Договор. ' * 400 + 'Авансовый платёж составляет 30 процентов стоимости.'))
    ranked, coverage = query_windows(chunks, 'какой аванс по договору')
    assert '30 процентов' in ranked[0][1] and coverage['searched_chunks'] == 71


def test_new_administrative_type_needs_no_new_template_code(db, demo):
    from bis.agents.tools_authoring import DraftArgs, create_document_draft
    ctx = context(db, demo)
    ctx.db.info['request_created_documents'] = []
    draft = create_document_draft.handler(ctx, DraftArgs(document_type='Служебное заключение',
        subject='О выполненной проверке', paragraphs=['Результат проверки: [уточнить по фактическим данным].']))
    document = db.get(Document, uuid.UUID(draft['id']))
    assert document.kind == 'administrative' and document.file_key
    assert document.data['authoring']['document_type'] == 'Служебное заключение'
    assert draft['basis']['mode'] == 'fresh_draft' and draft['basis']['review_required']
    from bis.agents.authoring_tasks import finalize
    from bis.agents.base import AgentResult
    finalize(ctx, AgentResult(text='Готово', agent='assistant'), 'Создай служебное заключение')
    assert ctx.draft_task['document_ids'] == [str(document.id)]
    assert ctx.draft_task['kind'] == 'Служебное заключение'


def test_examples_are_discovered_in_indexed_text_even_with_arbitrary_file_names(db, demo):
    from bis.rag.ingest import ingest_text
    original = sample(db, demo)
    original.kind, original.title, original.file_name = 'other', 'scan_0017', 'scan_0017.docx'
    db.flush()
    ingest_text(db, get_embedder(), org_id=demo.id, document_id=original.id,
                title=original.title, kind='org_document', text='Уважаемый коллега! Направляем исходящее письмо.')
    context(db, demo)
    assert str(original.id) in {item['id'] for item in authoring.examples(db, demo.id, 'letter')}


def test_brand_logo_is_copied_but_old_signature_image_is_not(db, demo):
    from PIL import Image
    original = sample(db, demo)
    word = Word(io.BytesIO(get_storage().get(original.file_key)))
    logo, signature = io.BytesIO(), io.BytesIO()
    Image.new('RGB', (16, 16), 'red').save(logo, format='PNG')
    Image.new('RGB', (16, 16), 'blue').save(signature, format='PNG')
    word.sections[0].header.paragraphs[0].add_run().add_picture(io.BytesIO(logo.getvalue()))
    word.add_paragraph().add_run().add_picture(io.BytesIO(signature.getvalue()))
    buffer = io.BytesIO()
    word.save(buffer)
    original.file_key = get_storage().put(buffer.getvalue(), 'with-logo.docx', prefix='authoring-test')
    db.flush()
    context(db, demo)
    fresh = authoring.render_administrative(demo, 'Письмо', 'Новая тема', ['Новый текст'], '', '', {})
    content, basis = authoring.adapt_layout(db, get_storage(), demo.id, 'letter', fresh)
    with ZipFile(io.BytesIO(content)) as package:
        media = [package.read(name) for name in package.namelist() if name.startswith('word/media/')]
    assert basis['mode'] == 'sender_brand_and_styles'
    assert logo.getvalue() in media and signature.getvalue() not in media


def test_disabled_base_is_not_used_as_an_example_even_outside_strict_mode(db, demo, tmp_path):
    import hashlib

    from bis.models.libraries import LibraryConnection, LibraryFile, LibraryVersion
    original = sample(db, demo)
    base = LibraryConnection(organization_id=demo.id, name='Примеры', path=str(tmp_path), level='organization', enabled=True)
    db.add(base)
    db.flush()
    item = LibraryFile(connection_id=base.id, relative_path='example.docx', state='ready')
    db.add(item)
    db.flush()
    version = LibraryVersion(file_id=item.id, document_id=original.id, revision=1, state='ready',
        file_key=original.file_key, sha256=hashlib.sha256(get_storage().get(original.file_key)).hexdigest(), mapping={})
    db.add(version)
    db.flush()
    item.active_version_id = version.id
    original.data = {**original.data, 'library_version_id': str(version.id)}
    db.flush()
    context(db, demo)
    assert str(original.id) in {item['id'] for item in authoring.examples(db, demo.id, 'letter')}
    base.enabled = False
    db.flush()
    assert str(original.id) not in {item['id'] for item in authoring.examples(db, demo.id, 'letter')}


def test_whole_available_text_does_not_mean_complete_original_extraction(db, demo):
    from bis.agents.tools_control import GetDocumentArgs, get_document
    from bis.rag.ingest import ingest_pages
    from bis.rag.parsers import Page
    doc = sample(db, demo)
    indexed = ingest_pages(db, get_embedder(), org_id=demo.id,
        pages=[Page(1, 'Читаемая страница договора. Цена договора составляет 40 485 945 рублей.'), Page(2, '', 'OCR не подключён')],
        kind='contract', title=doc.title, document_id=doc.id)
    ctx = context(db, demo)
    result = get_document.handler(ctx, GetDocumentArgs(document_id=str(doc.id)))
    assert result['complete'] is True and result['extraction_complete'] is False
    assert indexed.meta['reading']['unread_segments'] == [2]
    assert 'отсутствие' in result['extraction_note']


def test_cancelled_index_is_not_reused_as_current_document_text(db, demo):
    from bis.rag.ingest import ingest_text
    from bis.services.document_text import document_chunks
    doc = sample(db, demo)
    old = ingest_text(db, get_embedder(), org_id=demo.id, kind='contract', title='Старая редакция',
        text='Устаревшая сумма 999 рублей', document_id=doc.id)
    old.status = 'replaced'
    ingest_text(db, get_embedder(), org_id=demo.id, kind='contract', title='Текущая редакция',
        text='Текущая сумма 200 рублей', document_id=doc.id)
    chunks = document_chunks(db, doc)
    assert chunks and all('999' not in chunk.content for chunk in chunks)
