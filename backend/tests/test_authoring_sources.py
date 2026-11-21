"""Automatic discovery must inspect sources and retain project isolation."""
from test_adaptive_authoring import context, sample

from bis.agents.builtin_tools import LetterArgs, create_letter_draft, find_documents, get_organization_info
from bis.agents.grounding import authoring_sources
from bis.agents.tools_control import get_document
from bis.models import ConstructionObject
from bis.rag.embeddings import get_embedder
from bis.rag.ingest import ingest_text


def test_draft_without_sample_is_created_and_explicitly_marked(db, demo):
    ctx = context(db, demo)
    ctx.db.info['require_organization_letter_form'] = True
    result = create_letter_draft.handler(ctx, LetterArgs(subject='О проверке', body=['Проверка источников']))
    assert result['created']
    assert result['basis']['mode'] == 'fresh_draft'


def test_sources_find_contract_and_preserve_project_scope(db, demo):
    original = sample(db, demo, kind='contract')
    original.title = 'Договор для проверки реквизитов'
    ingest_text(db, get_embedder(), org_id=demo.id, document_id=original.id,
                title=original.title, kind='contract', text='Заказчик — ООО Контроль. Договор № 815.')
    project = ConstructionObject(organization_id=demo.id, name='Другой объект')
    db.add(project)
    db.flush()
    hidden = sample(db, demo, kind='contract')
    hidden.object_id = project.id
    hidden.title = 'Договор чужого объекта'
    generated = sample(db, demo, kind='contract')
    generated.source, generated.status = 'generated', 'draft'
    generated.title = 'Реквизиты заказчик подрядчик стороны договора'
    db.flush()
    ctx = context(db, demo)
    ctx.draft_task = {'kind': 'letter'}
    tools = {t.name: t for t in (find_documents, get_document, get_organization_info)}
    output = authoring_sources(ctx, tools, 'Напиши письмо заказчику')
    report = db.info['authoring_source_report']
    read_ids = {row['document']['id'] for row in report['documents']}
    assert str(original.id) in read_ids
    assert str(hidden.id) not in read_ids
    assert str(generated.id) not in read_ids
    assert all(row['document'].get('source_sha256') for row in report['documents'])
    assert 'ООО Контроль' in output
    assert not ctx.documents
    assert any(t['phase'] == 'authoring_sources' for t in ctx.trace)
