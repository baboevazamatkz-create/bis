import pytest
from sqlalchemy import select

from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
from bis.llm.providers.offline import ScriptedProvider
from bis.llm.types import Message, ToolCall
from bis.models import Document
from bis.rag.embeddings import get_embedder
from bis.services.archive import import_file
from bis.services.storage import get_storage


@pytest.mark.parametrize("explicit", [True, False])
def test_price_in_long_contract_returns_amount_not_whole_document(client, auth, db, demo, explicit):
    text = 'ДОГОВОР ПОДРЯДА № 42\n' + ('Описание работ на объекте.\n' * 400) + '\n3. Цена договора составляет 12 345 678,90 рублей, включая НДС.\nАванс 20 процентов.'
    imported = import_file(db, get_storage(), None, get_embedder(), org_id=demo.id, data=text.encode(), name='Договор42.txt')
    db.commit()
    result = client.post('/api/chat', headers=auth, json={'message': 'Сколько общая сумма по договору?' if explicit else 'Сколько общая сумма по договору № 42?', 'workspace': 'executive', 'document_id': imported['document_id'] if explicit else None})
    assert result.status_code == 200, result.text
    assert '12 345 678,90' in result.json()['text'], {'trace':result.json()['trace'], 'docs':[(d.kind,d.number,d.title) for d in db.scalars(select(Document)).all()]}
    assert len(result.json()['text']) < 1000
    assert 'смотрите сами' not in result.json()['text']


def test_letter_request_executes_write_even_when_norms_mentioned(client, auth, db, demo):
    responses = [Message('assistant',tool_calls=[ToolCall('delegate','delegate_to_agent',{'agent':'secretary','task':'Подготовь письмо о предоставлении сертификатов по требованиям договора'})]),
                 Message('assistant',tool_calls=[ToolCall('letter','create_letter_draft',{'addressee':['Заказчику'],'subject':'О предоставлении сертификатов','body':['Просим предоставить сертификаты на применённые материалы.']})]),
                 'Черновик подготовлен.', 'Письмо подготовлено.']
    llm = ScriptedProvider(responses)
    set_gateway(LLMGateway(entries={'test':ProviderEntry(llm,'confidential')}))
    result = client.post('/api/chat',headers=auth,json={'message':'Подготовь письмо заказчику с требованием предоставить сертификаты по договору','workspace':'executive'})
    assert result.status_code == 200, result.text
    doc = db.scalar(select(Document).where(Document.organization_id==demo.id,Document.kind=='letter'))
    assert doc and doc.file_key
    assert result.json()['documents']


def test_status_does_not_construct_or_wait_for_model_clients(client, auth, monkeypatch):
    from bis.llm import gateway

    def forbidden():
        raise AssertionError('Status must not create model clients')

    monkeypatch.setattr(gateway, 'get_gateway', forbidden)
    response = client.get('/api/workspace/reading-status', headers=auth)
    assert response.status_code == 200
    assert 'key_configured' in response.json()


def test_legacy_library_contract_price_cites_the_page_with_amount(client, auth, db, demo):
    from bis.acceptance import model_observed
    from bis.rag.ingest import ingest_pages
    from bis.rag.parsers import Page
    from bis.services.archive import repair_archive

    name = 'Договор субподряда 118.pdf'
    doc = Document(organization_id=demo.id, source='library', kind='other', status='registered',
                   title=name, file_name=name)
    db.add(doc)
    db.flush()
    source = ingest_pages(db, get_embedder(), org_id=demo.id, kind='org_document',
                          title=name, document_id=doc.id, pages=[
                              Page(1, 'Договор субподряда № 118\nСтороны заключили настоящий договор.'),
                              Page(8, '3.1. Цена договора составляет 42 761 950 руб. 00 копеек,\nбез учёта НДС.'),
                              Page(17, 'По договору предусмотрены общие правила расчётов.')])
    db.flush()
    assert repair_archive(db)['contracts'] == 1
    assert doc.kind == source.kind == 'contract'
    assert repair_archive(db)['contracts'] == 0
    db.commit()
    response = client.post('/api/chat', headers=auth, json={
        'message': 'Какова цена по договору и как учитывается НДС? Укажи страницу.',
        'workspace': 'executive', 'document_id': str(doc.id)})
    assert response.status_code == 200
    payload = response.json()
    assert '42 761 950' in payload['text'] and 'без учёта НДС' in payload['text']
    assert any(c['used'] and c['page'] == 8 and c['document_id'] == str(doc.id)
               and '42 761 950' in c['content'] for c in payload['citations'])
    assert not model_observed(payload)


def test_explicit_page_label_links_to_unique_fragment_from_same_document():
    from bis.agents.base import align_citation_pages, citation_refs

    fragments = [{'document_id': 'contract-a', 'page': 1},
                 {'document_id': 'contract-a', 'page': 9},
                 {'document_id': 'contract-b', 'page': 9}]
    text = 'Пять календарных дней [пункт на странице 9 договора][1].'
    fixed = align_citation_pages(text, fragments)
    assert fixed == 'Пять календарных дней [пункт на странице 9 договора][2].'
    assert citation_refs(fixed) == {2}
    assert align_citation_pages('См. стр. 2 [1].', fragments) == 'См. стр. 2 [1].'
    assert align_citation_pages(text, [*fragments, fragments[1]]) == text
