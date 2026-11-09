"""Fact provenance, versioning, conflicts and the pre-generation investigator."""
import datetime as dt
import uuid

import pytest
from sqlalchemy import delete, func, select
from test_executive_pipeline import context as context  # noqa: F401 — pytest fixtures
from test_executive_pipeline import corpus as corpus  # noqa: F401
from test_executive_pipeline import source

from bis.models import ConstructionObject, EstimateItem, KnowledgeSource, User, WorkVolume
from bis.models.facts import BrainInvestigation, FactDecision, FactSnapshot, ProjectFact
from bis.rag.embeddings import get_embedder
from bis.rag.ingest import ingest_pages, ingest_text
from bis.rag.parsers import Page
from bis.services import project_facts as ledger


def replace_text(ctx, doc, text):
    ctx.db.execute(delete(KnowledgeSource).where(KnowledgeSource.document_id == doc.id))
    doc.file_key = ctx.storage.put(text.encode(), doc.file_name, prefix='test:facts')
    ingest_text(ctx.db, get_embedder(), org_id=ctx.org_id, text=text, kind='org_document', title=doc.title, document_id=doc.id)
    ctx.db.flush()


def test_snapshot_is_grounded_and_idempotent(context, corpus):
    doc = corpus['journal']
    first = ledger.index_document(context, doc)
    second = ledger.index_document(context, doc)
    assert first.id == second.id and first.state == 'active'
    rows = context.db.scalars(select(ProjectFact).where(ProjectFact.snapshot_id == first.id)).all()
    assert any(f.predicate == 'work_end' and f.value == '2026-11-05' for f in rows)
    assert first.file_sha256 and first.text_sha256
    assert context.db.scalar(select(func.count()).select_from(FactSnapshot)) == 1
    assert all(f.quote and f.object_id == doc.object_id for f in rows)


def test_page_locator_is_from_real_chunk(context):
    doc = source(context, 'work_journal', 'Журнал участка 123', '05.11.2026 участок 123 завершена прокладка кабеля.')
    context.db.execute(delete(KnowledgeSource).where(KnowledgeSource.document_id == doc.id))
    ingest_pages(context.db, get_embedder(), org_id=context.org_id,
        pages=[Page(7, '05.11.2026 участок 123 завершена прокладка кабеля.')], kind='org_document', title=doc.title, document_id=doc.id)
    snapshot = ledger.index_document(context, doc)
    fact = context.db.scalar(select(ProjectFact).where(ProjectFact.snapshot_id == snapshot.id, ProjectFact.predicate == 'work_end'))
    assert fact.page == 7 and fact.chunk_id


def test_materials_keep_plan_actual_and_quality_separate(context, corpus):
    for key in ('project', 'journal', 'cert'):
        ledger.index_document(context, corpus[key])
    facts = ledger.query(context, predicate='material')['items']
    assert {f['category'] for f in facts} == {'planned', 'actual', 'reference'}
    assert any(f['value'] == 'кабель ВВГнг-LS 3х2,5' and f['category'] == 'planned' for f in facts)
    assert not ledger.reconcile(context)


def test_changed_document_never_leaves_old_assertions_current(context, corpus):
    doc = corpus['journal']
    original = ledger.index_document(context, doc)
    replace_text(context, doc, '06.11.2026 участок 123 завершена прокладка кабеля ВВГнг-LS 3х2,5.')
    assert not ledger.query(context, predicate='work_end')['items']
    latest = ledger.index_document(context, doc)
    assert latest.id != original.id
    context.db.refresh(original)
    assert original.state == 'superseded'
    assert {f['value'] for f in ledger.query(context, predicate='work_end')['items']} == {'2026-11-06'}


def conflicting_journal(context, corpus):
    for key in ('project', 'journal'):
        ledger.index_document(context, corpus[key])
    other = source(context, 'work_journal', 'Ещё один журнал участка 123',
        '06.11.2026 участок 123 завершена прокладка кабеля ВВГнг-LS 3х2,5.')
    ledger.index_document(context, other)
    conflict = next(c for c in ledger.reconcile(context) if c.predicate == 'work_end')
    return other, conflict


def test_conflict_choice_is_auditable_and_invalidates_on_new_source_version(context, corpus):
    other, conflict = conflicting_journal(context, corpus)
    rows = ledger.conflicts_out(context, [conflict])[0]['facts']
    selected = next(f for f in rows if f['value'] == '2026-11-05')
    ledger.decide(context, conflict.id, uuid.UUID(selected['id']), conflict.revision, 'Сверено с записью общего журнала')
    chosen = ledger.preferred_dates(context, 'участок 123', 'прокладка кабеля')['work_end']
    assert chosen['value'] == '2026-11-05' and chosen['decision_id']
    replace_text(context, other, '07.11.2026 участок 123 завершена прокладка кабеля ВВГнг-LS 3х2,5.')
    ledger.index_document(context, other)
    updated = next(c for c in ledger.reconcile(context) if c.id == conflict.id)
    assert updated.state == 'open'
    assert not ledger.preferred_dates(context, 'участок 123', 'прокладка кабеля')
    assert context.db.scalar(select(func.count()).select_from(FactDecision)) == 1


def test_material_mismatch_cannot_be_silenced_by_picking_a_value(context, corpus):
    ledger.index_document(context, corpus['project'])
    replace_text(context, corpus['journal'], '05.11.2026 участок 123 завершена прокладка кабеля ВВГнг-LS 3х4.')
    ledger.index_document(context, corpus['journal'])
    candidates = ledger.reconcile(context)
    assert any(c.kind == 'material_mismatch' for c in candidates), [(f['category'], f['value'], f['location_key'], f['work_key']) for f in ledger.query(context, predicate='material')['items']]
    conflict = next(c for c in candidates if c.kind == 'material_mismatch')
    with pytest.raises(ValueError, match='документ-основание'):
        ledger.decide(context, conflict.id, uuid.UUID(conflict.fact_ids[0]), conflict.revision, 'Выбрать материал')


def test_investigator_reads_journal_and_does_not_mix_neighboring_sections(context, corpus):
    from bis.brain.orchestrator import answer, investigate
    result = investigate(context, 'Когда завершили прокладку кабеля на участке 123?')
    assert result['state'] == 'ready', result
    assert result['plan']['predicates'] == ['work_end']
    assert {f['value'] for f in result['facts']} == {'2026-11-05'}
    text = answer(context, result)
    assert '05.11.2026' in text and '[' in text
    assert 'предоставьте' not in text.lower()
    assert context.db.get(BrainInvestigation, uuid.UUID(result['id'])).state == 'ready'
    second = investigate(context, 'Когда завершили прокладку кабеля на участке 123?')
    assert second['coverage']['cached'] == 1


def test_investigation_preserves_real_conflicting_values(context, corpus):
    from bis.brain.orchestrator import answer, investigate
    conflicting_journal(context, corpus)
    result = investigate(context, 'Когда завершили прокладку кабеля на участке 123?')
    assert result['state'] == 'conflict'
    text = answer(context, result)
    assert '05.11.2026' in text and '06.11.2026' in text


def test_missing_actual_material_does_not_fall_back_to_project_specification(context, corpus):
    from bis.brain.orchestrator import investigate
    context.db.delete(corpus['journal'])
    context.db.flush()
    result = investigate(context, 'Какие материалы использовали при прокладке кабеля на участке 123?')
    assert result['state'] == 'needs_data' and 'material' in result['missing']
    assert result['facts'] == []
    planned = investigate(context, 'Какие материалы предусмотрены проектом для прокладки кабеля на участке 123?')
    assert planned['state'] == 'ready' and planned['facts'][0]['category'] == 'planned'


def test_budget_is_visible_and_backfill_does_not_starve_later_documents(context, corpus):
    from bis.brain.orchestrator import investigate
    for n in range(4):
        source(context, 'work_journal', f'Журнал {n} участка 123', '05.11.2026 участок 123 завершена прокладка кабеля.')
    result = investigate(context, 'Когда завершили прокладку кабеля на участке 123?', budget=1)
    assert result['state'] == 'partial' and result['coverage']['remaining'] == 4
    first = ledger.refresh_project(context, limit=2)
    second = ledger.refresh_project(context, limit=20)
    assert first['remaining'] > 0 and second['remaining'] == 0
    assert investigate(context, 'Когда завершили прокладку кабеля на участке 123?', budget=1)['state'] == 'ready'


def test_unread_document_is_distinct_from_absent_data(context, corpus):
    from bis.brain.orchestrator import answer, investigate
    doc = corpus['journal']
    context.db.execute(delete(KnowledgeSource).where(KnowledgeSource.document_id == doc.id))
    doc.file_key = context.storage.put(b'', 'empty.txt', prefix='tests:facts')
    context.db.flush()
    result = investigate(context, 'Когда завершили прокладку кабеля на участке 123?')
    assert result['state'] == 'partial'
    assert 'Не прочитан файл' in answer(context, result)


def test_project_and_tenant_guards_apply_even_without_strict_libraries(context, corpus):
    first = ledger.index_document(context, corpus['journal'])
    other = ConstructionObject(organization_id=context.org_id, name='Отдельный объект')
    context.db.add(other)
    context.db.flush()
    from bis.services.library_access import bind_context
    bind_context(context.db, context.org_id, other.id)
    context.focus = {'object': str(other.id)}
    assert ledger.query(context, other.id)['items'] == []
    assert context.db.scalar(select(FactSnapshot.id).where(FactSnapshot.id == first.id)) is None
    with pytest.raises(LookupError):
        ledger.scope(context, corpus['journal'].object_id)


def test_registry_volumes_are_not_confused_with_completion(context, corpus):
    from bis.brain.orchestrator import answer, investigate
    project_id = corpus['project'].object_id
    item = context.db.scalar(select(EstimateItem).where(EstimateItem.object_id == project_id))
    item.name, item.unit = 'Прокладка кабеля', 'м'
    rows = [WorkVolume(organization_id=context.org_id, object_id=project_id, estimate_item_id=item.id,
        date=dt.date(2026, 11, day), quantity=quantity, location='участок 999', status='confirmed') for day, quantity in ((1, 10), (2, 15))]
    context.db.add_all(rows)
    context.db.flush()
    result = investigate(context, 'Какой объём прокладки кабеля на участке 999?')
    assert result['state'] == 'ready', result
    assert '25 м' in answer(context, result)
    completion = investigate(context, 'Когда завершили прокладку кабеля на участке 999?')
    assert completion['state'] == 'needs_data' and not completion['facts']
    rows[0].quantity = 20
    context.db.flush()
    quantity = ledger.query(context, predicate='quantity', location='участок 999')['items']
    assert {f['value'] for f in quantity} == {'15'}


def test_chat_answers_from_facts_before_model_and_keeps_followup_context(client, auth, context, corpus):
    context.db.commit()
    project_id = str(corpus['journal'].object_id)
    headers = {**auth, 'X-BIS-Project': project_id}
    first = client.post('/api/chat', headers=headers, json={'message': 'Когда завершили прокладку кабеля на участке 123?', 'workspace': 'executive'})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body['provider'] == 'project_facts' and '05.11.2026' in body['text']
    follow = client.post('/api/chat', headers=headers, json={'message': 'А когда начали?', 'session_id': body['session_id'], 'workspace': 'executive'})
    assert follow.status_code == 200, follow.text
    assert '01.11.2026' in follow.json()['text'] and '01.10.2026' not in follow.json()['text']


def test_fact_api_roles_scope_and_snapshot_original(client, auth, context, corpus):
    first = ledger.index_document(context, corpus['journal'])
    context.db.commit()
    headers = {**auth, 'X-BIS-Project': str(first.object_id)}
    result = client.get('/api/brain/facts?predicate=work_end&location=участок%20123', headers=headers)
    assert result.status_code == 200 and result.json()['items']
    proof = client.get(f'/api/brain/facts/snapshots/{first.id}/file', headers=headers)
    assert proof.status_code == 200 and '05.11.2026' in proof.content.decode()
    user = context.db.get(User, context.user.id)
    user.role = 'viewer'
    context.db.commit()
    assert client.post('/api/brain/facts/refresh', headers=headers, json={}).status_code == 403


def test_certificate_date_never_becomes_the_work_completion_date():
    from bis.services.executive_pipeline import heuristic_report
    report = heuristic_report('Проложен кабель на участке 123 с сертификатом, действительным до 31.12.2026.', dt.date(2026, 11, 9))
    assert report.work_end is None


def test_inline_start_end_dates_are_not_interchanged(context):
    from bis.services.executive_pipeline import Fact, verified_fact
    text = 'Участок 123, прокладка кабеля: начало 01.11.2026; окончание 05.11.2026.'
    doc = source(context, 'work_journal', 'Журнал участка 123', text)
    ledger.index_document(context, doc)
    facts = ledger.query(context)['items']
    assert any(f['predicate'] == 'work_start' and f['value'] == '2026-11-01' for f in facts)
    assert any(f['predicate'] == 'work_end' and f['value'] == '2026-11-05' for f in facts)
    assert not verified_fact(Fact(field='work_start', value='2026-11-05', quote=text), text)


def test_impossible_period_is_not_a_ready_answer(context):
    from bis.brain.orchestrator import investigate
    source(context, 'work_journal', 'Журнал участка 123',
        '06.11.2026 участок 123 начата прокладка кабеля.\n05.11.2026 участок 123 завершена прокладка кабеля.')
    result = investigate(context, 'Когда завершили прокладку кабеля на участке 123?')
    assert result['state'] == 'conflict'
    assert any(c['kind'] == 'invalid_period' for c in result['conflicts'])


def test_hallucinated_model_value_is_not_in_the_ledger(context, corpus):
    import json

    from bis.llm.gateway import LLMGateway, ProviderEntry
    from bis.llm.providers.offline import ScriptedProvider
    provider = ScriptedProvider([json.dumps({'facts': [{'field': 'material', 'value': 'кабель ВВГ 3х95', 'quote': 'Несуществующая цитата'}]})])
    context.gateway = LLMGateway(entries={'model': ProviderEntry(provider, 'confidential')})
    snapshot = ledger.index_document(context, corpus['project'])
    assert snapshot.state == 'partial'
    assert not any('3х95' in f['value'] for f in ledger.query(context)['items'])


def test_repeated_structured_volume_snapshot_has_stable_identity_after_reload(context, corpus):
    project_id = corpus['project'].object_id
    item = context.db.scalar(select(EstimateItem).where(EstimateItem.object_id == project_id))
    row = WorkVolume(organization_id=context.org_id, object_id=project_id, estimate_item_id=item.id,
        date=dt.date(2026, 11, 1), quantity=15, location='участок 999', status='confirmed')
    context.db.add(row)
    context.db.flush()
    ledger.scope(context, project_id)
    ledger.index_work_volumes(context)
    first = context.db.scalar(select(FactSnapshot.id).where(FactSnapshot.work_volume_id == row.id))
    context.db.refresh(row)
    ledger.index_work_volumes(context)
    assert context.db.scalar(select(func.count()).select_from(FactSnapshot).where(FactSnapshot.work_volume_id == row.id)) == 1
    assert context.db.scalar(select(FactSnapshot.id).where(FactSnapshot.work_volume_id == row.id)) == first


def test_issued_act_rechecks_engineer_decision(context, corpus):
    from bis.models import Document
    from bis.services.executive_pipeline import validate_sources
    other, conflict = conflicting_journal(context, corpus)
    facts = ledger.conflicts_out(context, [conflict])[0]['facts']
    selected = next(f for f in facts if f['value'] == '2026-11-05')
    ledger.decide(context, conflict.id, uuid.UUID(selected['id']), conflict.revision, 'Сверено с подписанным журналом')
    preferred = ledger.preferred_dates(context, 'участок 123', 'прокладка кабеля')['work_end']
    act = Document(organization_id=context.org_id, object_id=corpus['journal'].object_id,
        kind='aosr', title='Проверяемый акт', status='draft', data={'evidence': [{
            'fact_id': preferred['id'], 'decision_id': preferred['decision_id']}]})
    context.db.add(act)
    context.db.flush()
    validate_sources(context.db, context.storage, act)
    ledger.decide(context, conflict.id, uuid.UUID(next(f['id'] for f in facts if f['value'] == '2026-11-06')),
        conflict.revision, 'Сверено с исправлением журнала')
    with pytest.raises(ValueError, match='выбора инженера изменилось'):
        validate_sources(context.db, context.storage, act)


def test_changed_source_invalidates_act_choice_before_reextraction(context, corpus):
    from bis.models import Document
    from bis.services.executive_pipeline import validate_sources
    other, conflict = conflicting_journal(context, corpus)
    selected = ledger.conflicts_out(context, [conflict])[0]['facts'][0]
    ledger.decide(context, conflict.id, uuid.UUID(selected['id']), conflict.revision, 'Сверено с подписанным журналом')
    preferred = ledger.preferred_dates(context, 'участок 123', 'прокладка кабеля')['work_end']
    act = Document(organization_id=context.org_id, object_id=corpus['journal'].object_id,
        kind='aosr', title='Проверяемый акт', status='draft', data={'evidence': [{
            'fact_id': preferred['id'], 'decision_id': preferred['decision_id']}]})
    context.db.add(act)
    context.db.flush()
    replace_text(context, other, '07.11.2026 участок 123 завершена прокладка кабеля.')
    with pytest.raises(ValueError, match='выбора инженера изменилось'):
        validate_sources(context.db, context.storage, act)


@pytest.mark.parametrize('text', [
    '05.11.2026 участок 123 прокладка кабеля не завершена.',
    '05.11.2026 участок 123 планируется начало прокладки кабеля.',
    '05.11.2026 участок 123 кабель не проложен.',
])
def test_negative_and_planned_journal_statements_do_not_prove_actual_work(context, text):
    doc = source(context, 'work_journal', 'Журнал участка 123', text)
    ledger.index_document(context, doc)
    facts = ledger.query(context)['items']
    assert not [f for f in facts if f['predicate'] in ('work_start', 'work_end', 'work')]


def test_unknown_investigation_does_not_dump_unrelated_project_facts(context, corpus):
    from bis.brain.orchestrator import answer, investigate
    result = investigate(context, 'Объясни финансовую стратегию')
    assert result['state'] == 'unsupported'
    assert result['coverage']['candidates'] == 0
    assert 'диалог' in answer(context, result)


def test_inventory_questions_keep_specialized_calculation_route():
    from bis.brain.orchestrator import question_plan
    assert not question_plan('Какой перерасход материалов за май 2026?')['predicates']
    assert question_plan('Какие материалы применены на участке 123?')['predicates'] == ['material']


def test_pipeline_uses_shared_decision_and_keeps_it_in_act_evidence(context, corpus):
    from test_executive_pipeline import run

    from bis.models import Document
    _, conflict = conflicting_journal(context, corpus)
    selected = next(f for f in ledger.conflicts_out(context, [conflict])[0]['facts'] if f['value'] == '2026-11-05')
    ledger.decide(context, conflict.id, uuid.UUID(selected['id']), conflict.revision, 'Сверено с подписанной записью журнала')
    event = run(context)
    assert event.result['status'] == 'ready_for_review', event.result['gaps']
    doc = context.db.get(Document, event.document_id)
    assert doc.data['work_end'] == '2026-11-05'
    assert any(e.get('fact_id') == selected['id'] and e.get('decision_id') for e in doc.data['evidence'])


def test_creation_commands_keep_document_generation_route():
    from bis.brain.orchestrator import question_plan
    assert not question_plan('Сделай АОСР, укажи материалы и дату окончания')['predicates']
    assert question_plan('Какая дата окончания прокладки кабеля?')['predicates'] == ['work_end']


def test_chat_session_cannot_carry_facts_into_other_project_without_strict_mode(client, auth, context, corpus):
    other = ConstructionObject(organization_id=context.org_id, name='Другой проект', code='FACTS-OTHER')
    context.db.add(other)
    context.db.commit()
    first = client.post('/api/chat', headers={**auth, 'X-BIS-Project': str(corpus['journal'].object_id)},
        json={'message': 'Когда завершили прокладку кабеля на участке 123?', 'workspace': 'executive'})
    assert first.status_code == 200, first.text
    response = client.post('/api/chat', headers={**auth, 'X-BIS-Project': str(other.id)},
        json={'message': 'А когда начали?', 'workspace': 'executive', 'session_id': first.json()['session_id']})
    assert response.status_code == 404
