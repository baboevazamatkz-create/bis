"""Outcomes of multi-turn orchestration on the production chat endpoint."""
import io
import uuid

from docx import Document as Word
from sqlalchemy import select

from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
from bis.llm.providers.offline import ScriptedProvider
from bis.llm.types import Message, ToolCall
from bis.models import ConstructionObject, Document
from bis.rag.embeddings import get_embedder
from bis.rag.ingest import ingest_text


def _turn(client, auth, message, project=None, session=None):
    response = client.post('/api/chat', headers=auth, json={
        'message': message, 'workspace': 'executive', 'object_id': project, 'session_id': session,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_incomplete_aosr_resumes_and_returns_one_real_file(client, auth, db, demo):
    project = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    project.name = 'Хандыга'
    design = Document(organization_id=demo.id, object_id=project.id, kind='project_doc', source='uploaded',
                      title='Армирование плиты', status='registered')
    db.add(design)
    db.flush()
    ingest_text(db, get_embedder(), org_id=demo.id, document_id=design.id, title=design.title,
                kind='org_document', text='Армирование плиты.\nШифр РД: РД-Х-17, лист 5.\nАрматура А500С предусмотрена проектом.')
    db.commit()
    project_id = str(project.id)
    model = ScriptedProvider([Message(role='assistant', tool_calls=[
        ToolCall('wrong-doc', 'create_document_draft', {'document_type': 'приказ', 'subject': 'Армирование',
                                                      'paragraphs': ['Составить акт.']})]), 'Документ создан.'])
    set_gateway(LLMGateway(entries={'test': ProviderEntry(model, 'confidential')}))
    first = _turn(client, auth, 'Подготовь АОСР по загруженным документам на армирование плиты', project_id)
    session = first['session_id']
    extraction_calls = len(model.calls)
    assert not first['documents']
    confirmed = _turn(client, auth, 'Создай из того, что есть, остальное я сам допишу', project_id, session)
    assert len(confirmed['documents']) == 1
    identifier = confirmed['documents'][0]['id']
    saved = db.get(Document, uuid.UUID(identifier))
    assert saved.kind == 'aosr' and saved.status == 'draft'
    assert saved.data['work_start'] is None and saved.data['work_end'] is None
    assert not saved.data['materials']
    assert 'РД-Х-17' in saved.data['project_docs']
    assert 'completion' in {issue['field'] for issue in saved.issues}
    for message in ('да', 'пришли файл'):
        next_turn = _turn(client, auth, message, project_id, session)
        assert [ref['id'] for ref in next_turn['documents']] == [identifier]
        assert not next_turn['trace']
    response = client.get(f'/api/documents/{identifier}/file', headers=auth)
    assert response.status_code == 200
    word = Word(io.BytesIO(response.content))
    assert not any(any(date in p.text for date in ('0001-01-01', '01.01.0001', 'января 1 г.')) for p in word.paragraphs)
    related = list(db.scalars(select(Document).where(Document.object_id == project.id, Document.source == 'generated')))
    assert [doc.id for doc in related] == [saved.id]
    assert len(model.calls) == extraction_calls
    assert all(not call['tools'] for call in model.calls)


def test_identical_failed_calls_execute_only_once_per_turn(client, auth):
    missing = str(uuid.uuid4())
    call = lambda identifier: Message(role='assistant', tool_calls=[  # noqa: E731
        ToolCall(identifier, 'get_document', {'document_id': missing})])
    model = ScriptedProvider([call('first'), call('duplicate'), 'Документ не найден.'])
    set_gateway(LLMGateway(entries={'test': ProviderEntry(model, 'confidential')}))
    payload = _turn(client, auth, 'Прочитай приложенный файл')
    calls = [event for event in payload['trace'] if event['tool'] == 'get_document']
    assert calls[0]['error'] and not calls[0].get('skipped')
    assert calls[1]['skipped'] and calls[1]['error_kind'] == 'duplicate'


def test_text_only_model_uses_supported_text_tool_adapter(client, auth):
    model = ScriptedProvider(['get_organization_info {}', 'Реквизиты найдены.'])
    set_gateway(LLMGateway(entries={'test': ProviderEntry(model, 'confidential', capabilities=('text',))}))
    answer = _turn(client, auth, 'Какие реквизиты у нашей организации?')
    assert any(event['tool'] == 'get_organization_info' and not event['error'] for event in answer['trace'])
    assert all(not call['tools'] for call in model.calls)
    assert any('7701000001' in msg.content for msg in model.calls[-1]['messages'])


def test_permission_does_not_move_between_projects(client, auth, db, demo):
    first_project = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    second_project = ConstructionObject(organization_id=demo.id, name='Чужой участок того же предприятия')
    db.add(second_project)
    db.commit()
    first = _turn(client, auth, 'Нужно подготовить акт скрытых работ по армированию плиты', str(first_project.id))
    assert not first['documents']
    denied = client.post('/api/chat', headers=auth, json={
        'message': 'да', 'session_id': first['session_id'], 'workspace': 'executive',
        'object_id': str(second_project.id),
    })
    assert denied.status_code == 404
    correct = _turn(client, auth, 'да', str(first_project.id), first['session_id'])
    assert [ref['kind'] for ref in correct['documents']] == ['aosr']


def test_corrupt_file_never_returned_as_success_and_cannot_trigger_another_write(client, auth, db, demo):
    from bis.services.storage import get_storage

    project = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    db.commit()
    asked = _turn(client, auth, 'Оформи АОСР на армирование плиты: оставь даты пустыми', str(project.id))
    assert len(asked['documents']) == 1
    saved = db.get(Document, uuid.UUID(asked['documents'][0]['id']))
    saved.file_key = get_storage().put(b'PK corrupt', 'bad.docx', prefix='orchestrator-test')
    db.commit()
    result = _turn(client, auth, 'пришли', str(project.id), asked['session_id'])
    assert not result['documents'] and 'недоступен' in result['text']
    assert len(list(db.scalars(select(Document.id).where(
        Document.organization_id == demo.id, Document.kind == 'aosr', Document.source == 'generated')))) == 1


def test_conflicting_document_tool_is_rejected_then_right_tool_runs_once(client, auth, db):
    call = {'subject': 'О сроках работ', 'body': ['Просим согласовать сроки.']}
    model = ScriptedProvider([
        Message(role='assistant', tool_calls=[ToolCall('bad', 'create_document_draft', {
            'document_type': 'приказ', 'subject': 'Неверный результат', 'paragraphs': ['Создать приказ.'],
        })]),
        Message(role='assistant', tool_calls=[
            ToolCall('letter', 'create_letter_draft', call), ToolCall('retry', 'create_letter_draft', call),
        ]),
        'Письмо подготовлено.',
    ])
    set_gateway(LLMGateway(entries={'test': ProviderEntry(model, 'confidential')}))
    answer = _turn(client, auth, 'Подготовь письмо о сроках работ на основе приказа')
    failures = [event for event in answer['trace'] if event['tool'] == 'create_document_draft']
    assert failures[0]['error_kind'] == 'incompatible'
    assert [ref['kind'] for ref in answer['documents']] == ['letter']
    assert [event.get('skipped', False) for event in answer['trace'] if event['tool'] == 'create_letter_draft'] == [False, True]
    assert list(db.scalars(select(Document.kind).where(Document.id == uuid.UUID(answer['documents'][0]['id'])))) == ['letter']


def test_text_of_aook_does_not_grant_loading_without_evidence(client, auth, db, demo):
    project = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    db.commit()
    result = _turn(client, auth, 'Сформируй АООК на фундаментную плиту, оставь пропуски', str(project.id))
    saved = db.get(Document, uuid.UUID(result['documents'][0]['id']))
    assert saved.kind == 'aook' and saved.data['permit'] is None
    assert 'permit' in {issue['field'] for issue in saved.issues}
    document = client.get(f'/api/documents/{saved.id}/file', headers=auth)
    text = '\n'.join(p.text for p in Word(io.BytesIO(document.content)).paragraphs)
    assert 'Решение о нагружении' in text and 'разрешается использование конструкций' not in text


def test_denial_and_unrelated_yes_cannot_authorize_draft(client, auth, db, demo):
    project = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    project_id = str(project.id)
    first = _turn(client, auth, 'Подготовь АОСР на армирование плиты', project_id)
    session = first['session_id']
    stopped = _turn(client, auth, 'Нет, не создавай из того, что есть', project_id, session)
    assert stopped['text'] == 'Подготовка остановлена.'
    yes = _turn(client, auth, 'да', project_id, session)
    assert not yes['documents']
    assert not db.scalar(select(Document.id).where(Document.kind == 'aosr', Document.source == 'generated'))


def test_reference_act_does_not_replace_requested_scheme():
    from bis.agents.authoring_tasks import intent

    assert intent('Подготовь проект исполнительной схемы на основании АОСР') == 'scheme'


def test_partial_permission_is_not_a_description_of_works(client, auth, db, demo):
    project = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    result = _turn(client, auth, 'Оформи АОСР, оставь даты пустыми', str(project.id))
    assert not result['documents']
    assert 'Укажите работы' in result['text']


def test_chat_uses_explicit_approved_form_version(client, auth, db, demo, monkeypatch, tmp_path):
    from test_libraries import _aosr_precision_doc

    from bis.config import get_settings

    monkeypatch.setattr(get_settings(), 'library_roots', [tmp_path])
    project = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    project_id = str(project.id)
    db.commit()
    uploaded = client.post('/api/libraries/upload', headers=auth, data={'level': 'organization'},
                           files={'files': ('Сроки.docx', _aosr_precision_doc('inline'))})
    assert uploaded.status_code == 201, uploaded.text
    connection = uploaded.json()['id']
    version = client.get(f'/api/libraries/{connection}/files', headers=auth).json()[0]['active_version_id']
    route = f'/api/libraries/versions/{version}'
    prepared = client.post(route + '/prepare?kind=aosr', headers=auth).json()
    mapping = {'kind': 'aosr', 'mapping': {key: prepared[key] for key in ('format', 'entries', 'profile')}}
    preview = client.post(route + '/preview', headers=auth, json=mapping)
    assert preview.status_code == 200, preview.text
    mapping['preview_token'] = preview.headers['X-BIS-Form-Preview']
    assert client.post(route + '/approve', headers=auth, json=mapping).status_code == 200
    result = client.post('/api/chat', headers=auth, json={
        'message': 'Подготовь АОСР на армирование плиты, оставь даты пустыми',
        'workspace': 'executive', 'object_id': project_id, 'form_version_id': version,
    })
    assert result.status_code == 200, result.text
    saved = db.get(Document, uuid.UUID(result.json()['documents'][0]['id']))
    assert saved.data['library_form']['version_id'] == version
    docx = client.get(f'/api/documents/{saved.id}/file', headers=auth)
    assert 'Сроки работ' in [p.text for p in Word(io.BytesIO(docx.content)).paragraphs]
