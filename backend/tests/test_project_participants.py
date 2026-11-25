from sqlalchemy import select

from bis.models import Appointment, ConstructionObject, Contract, Counterparty, Document
from bis.rag.embeddings import get_embedder
from bis.rag.ingest import ingest_text
from bis.services.documents import object_participants

PATH = '/api/workspace/project-structure'


def test_confirmation_updates_cards_used_by_acts_and_letters(client, auth, db, demo):
    project = ConstructionObject(organization_id=demo.id, name='Северная площадка')
    db.add(project)
    db.flush()
    file = Document(organization_id=demo.id, object_id=project.id, kind='contract',
                    title='Договор генподряда № 724', source='uploaded')
    db.add(file)
    db.flush()
    contract = Contract(organization_id=demo.id, object_id=project.id, kind='general', our_role='contractor',
                        number='724', document_id=file.id)
    db.add(contract)
    ingest_text(db, get_embedder(), org_id=demo.id, document_id=file.id, title=file.title,
                kind='org_document', text='Реквизиты сторон\nЗаказчик\nООО «Север»\nИНН: 7722446688')
    db.commit()
    headers = {**auth, 'X-BIS-Project': str(project.id)}
    response = client.get(PATH, headers=headers)
    assert response.status_code == 200, response.text
    draft = response.json()
    customer = next(p for p in draft['parties'] if p['role'] == 'developer' and p['name'] == 'ООО «Север»')
    assert customer['sources'][0]['document_id'] == str(file.id)
    assert project.developer_id is None
    customer['address'] = 'ул. Северная, 1'
    draft['project']['code'] = 'С-724'
    draft['people'].append({'full_name': 'Иванов Иван Иванович', 'position': 'Инженер',
                            'role': 'developer_control', 'party_key': customer['key'], 'department': 'ПТО'})
    draft['parties'].append({'role': 'subcontractor', 'name': 'ООО Монтаж', 'email': 'work@example.org'})
    confirmed = client.put(PATH, headers=headers, json=draft)
    assert confirmed.status_code == 200, confirmed.text
    assert client.put(PATH, headers=headers, json=draft).status_code == 409

    db.expire_all()
    assert project.code == 'С-724'
    assert db.get(Counterparty, project.developer_id).address == 'ул. Северная, 1'
    assert contract.counterparty_id == project.developer_id
    appointment = db.scalar(select(Appointment).where(Appointment.object_id == project.id))
    assert appointment.person.full_name == 'Иванов Иван Иванович'
    assert confirmed.json()['people'][0]['department'] == 'ПТО'
    participants = object_participants(db, demo.id, project.id)
    assert participants['developer'].inn == '7722446688'
    assert participants['representatives']['developer_control'].position == 'Инженер'
    letter = client.post('/api/documents/letter', headers=headers, json={
        'recipient_role': 'subcontractor', 'subject': 'О подготовке работ', 'body': ['Подготовьте график.'],
    })
    assert letter.status_code == 201, letter.text
    assert 'ООО Монтаж' in letter.json()['data']['addressee']


def test_contract_recipients_are_scoped_and_ambiguous_roles_are_left_blank(client, auth, db, demo):
    project = ConstructionObject(organization_id=demo.id, name='Монтаж')
    first = Counterparty(organization_id=demo.id, name='ООО Первый')
    second = Counterparty(organization_id=demo.id, name='ООО Второй')
    db.add_all([project, first, second])
    db.flush()
    for number, cp in [('М-01', first), ('М-02', second)]:
        db.add(Contract(organization_id=demo.id, object_id=project.id, counterparty_id=cp.id,
                        kind='subcontract', our_role='customer', number=number))
    db.commit()
    headers = {**auth, 'X-BIS-Project': str(project.id)}
    proposal = client.get(PATH, headers=headers).json()
    assert {p['name'] for p in proposal['parties'] if p['role'] == 'subcontractor'} == {'ООО Первый', 'ООО Второй'}
    letter = {'recipient_role': 'subcontractor', 'subject': 'О работах', 'body': ['Представьте график.']}
    ambiguous = client.post('/api/documents/letter', headers=headers, json=letter)
    assert ambiguous.status_code == 201, ambiguous.text
    assert ambiguous.json()['data']['addressee'] == ['[Адресат: уточнить по договору]']
    selected = client.post('/api/documents/letter', headers=headers, json={**letter, 'contract_id': 'М-02'})
    assert selected.status_code == 201, selected.text
    assert 'ООО Второй' in selected.json()['data']['addressee']


def test_empty_structure_can_be_confirmed(client, auth, db, demo):
    project = ConstructionObject(organization_id=demo.id, name='Без исходных данных')
    db.add(project)
    db.commit()
    headers = {**auth, 'X-BIS-Project': str(project.id)}
    response = client.put(PATH, headers=headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()['confirmed_at']
    assert project.developer_id is None


def test_organization_library_and_orders_fill_missing_details(client, auth, db, demo):
    project = ConstructionObject(organization_id=demo.id, name='Развязка')
    contractor = Counterparty(organization_id=demo.id, name='ООО «Путь»')
    db.add_all([project, contractor])
    db.flush()
    contract = Contract(organization_id=demo.id, object_id=project.id, counterparty_id=contractor.id,
                        kind='general', our_role='customer', number='П-1')
    db.add(contract)
    demo.email = None
    demo.phone = None
    files = [
        (None, 'other', 'Карточка организации', {'library_scope': 'organization'},
         f'Полное наименование: {demo.name}\nИНН: {demo.inn}\nEmail: office@example.org\nТелефон: +7 900 200-30-40'),
        (project.id, 'order', 'Приказ о назначении', {},
         'Ответственное лицо: Смирнов Сергей Сергеевич\nОрганизация: ООО «Путь»\n'
         'Должность: Инженер\nОтдел: Стройконтроль\nРоль: builder_control\nНомер приказа: 19'),
    ]
    for object_id, kind, title, data, contents in files:
        doc = Document(organization_id=demo.id, object_id=object_id, kind=kind, title=title, source='uploaded', data=data)
        db.add(doc)
        db.flush()
        ingest_text(db, get_embedder(), org_id=demo.id, document_id=doc.id,
                    title=title, kind='org_document', text=contents)
    db.commit()
    headers = {**auth, 'X-BIS-Project': str(project.id)}
    draft = client.get(PATH, headers=headers).json()
    own = next(p for p in draft['parties'] if p['role'] == 'organization')
    assert own['email'] == 'office@example.org'
    assert own['sources'][0]['level'] == 'organization'
    assert draft['people'][0]['department'] == 'Стройконтроль'
    assert draft['people'][0]['sources'][0]['level'] == 'project'
    own['phone'] = '+7 900 111-22-33'
    confirmed = client.put(PATH, headers=headers, json=draft)
    assert confirmed.status_code == 200, confirmed.text

    db.expire_all()
    assert demo.email == 'office@example.org'
    assert demo.phone == '+7 900 111-22-33'
    participants = object_participants(db, demo.id, project.id)
    assert participants['builder'].name == 'ООО «Путь»'
    assert participants['representatives']['builder_control'].organization == 'ООО «Путь»'


def test_inspection_act_uses_customer_and_signers_on_act_date(client, auth, db, demo):
    import uuid

    from test_adaptive_authoring import context

    from bis.agents.tools_authoring import DraftArgs, create_document_draft

    project = ConstructionObject(organization_id=demo.id, name='Площадка осмотра')
    db.add(project)
    db.commit()
    headers = {**auth, 'X-BIS-Project': str(project.id)}
    response = client.put(PATH, headers=headers, json={
        'parties': [{'key': 'owner', 'role': 'developer', 'name': 'ООО Заказчик', 'inn': '7766554433', 'kpp': '776601001'}],
        'people': [
            {'party_key': 'owner', 'role': 'developer_control', 'full_name': 'Иванов Иван Иванович',
             'valid_to': '2025-12-31'},
            {'party_key': 'owner', 'role': 'developer_control', 'full_name': 'Петров Пётр Петрович',
             'valid_from': '2026-01-01'},
            {'role': 'builder', 'full_name': 'Неустановленный сотрудник'},
        ],
    })
    assert response.status_code == 200, response.text
    assert sum(bool(p['appointment_id']) for p in response.json()['people']) == 2
    db.expire_all()
    ctx = context(db, demo)
    ctx.focus['object'] = str(project.id)
    act = create_document_draft.handler(ctx, DraftArgs(document_type='Акт осмотра', subject='Осмотр площадки',
                        paragraphs=['Осмотрели площадку.'], document_date='2025-11-01'))
    document = db.get(Document, uuid.UUID(act['id']))
    assert 'ООО Заказчик' in document.data['fields']['Заказчик']
    assert 'КПП 776601001' in document.data['fields']['Заказчик']
    assert 'Иванов' in document.data['fields']['Подписанты по назначениям']
    assert 'Петров' not in document.data['fields']['Подписанты по назначениям']



def test_a_project_cannot_confirm_another_projects_appointment(client, auth, db, demo):
    from bis.models import Person

    first = ConstructionObject(organization_id=demo.id, name='Первый объект')
    second = ConstructionObject(organization_id=demo.id, name='Второй объект')
    person = Person(organization_id=demo.id, full_name='Иванов Иван')
    db.add_all([first, second, person])
    db.flush()
    appointment = Appointment(organization_id=demo.id, object_id=second.id, role='builder', person_id=person.id)
    db.add(appointment)
    db.commit()
    headers = {**auth, 'X-BIS-Project': str(first.id)}
    response = client.put(PATH, headers=headers, json={
        'project': {'code': 'Изменение не должно примениться'},
        'people': [{'appointment_id': str(appointment.id), 'full_name': 'Другое лицо'}],
    })
    assert response.status_code == 422
    db.expire_all()
    assert first.code is None
    assert person.full_name == 'Иванов Иван'
