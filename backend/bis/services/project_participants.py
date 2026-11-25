"""Review project participants against source documents and apply them to BIS registries."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from collections import defaultdict
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import or_, select

from bis.brain.parties import parse_parties
from bis.documents.aosr import REPRESENTATIVE_ROLES
from bis.models import Appointment, ConstructionObject, Contract, Counterparty, Document, Organization, Person
from bis.services.archive import contract_kind
from bis.services.audit import audit
from bis.services.document_text import document_chunks
from bis.services.library_access import SourceContext, document_predicate, organization_documents


class Role(StrEnum):
    organization = 'organization'
    developer = 'developer'
    contractor = 'contractor'
    subcontractor = 'subcontractor'
    designer = 'designer'
    supplier = 'supplier'
    other = 'other'


Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
Long = Annotated[str, StringConstraints(strip_whitespace=True)]


class ProjectFields(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None
    code: Short | None = None
    address: Long | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None


class PartyFields(BaseModel):
    key: str | None = None
    counterparty_id: uuid.UUID | None = None
    own: bool = False
    role: Role = Role.other
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    inn: Annotated[str, StringConstraints(strip_whitespace=True, max_length=12)] | None = None
    kpp: Annotated[str, StringConstraints(strip_whitespace=True, max_length=9)] | None = None
    ogrn: Annotated[str, StringConstraints(strip_whitespace=True, max_length=15)] | None = None
    address: Long | None = None
    phone: Short | None = None
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    sro: Long | None = None
    head_name: Title | None = None
    head_position: Title | None = None
    department: Title | None = None


class PersonFields(BaseModel):
    appointment_id: uuid.UUID | None = None
    party_key: str | None = None
    person_id: uuid.UUID | None = None
    full_name: Title | None = None
    position: Title | None = None
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    phone: Short | None = None
    nrs_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)] | None = None
    department: Title | None = None
    role: Short | None = None
    order_number: Short | None = None
    order_date: dt.date | None = None
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None


class Confirmation(BaseModel):
    revision: str | None = None
    project: ProjectFields = Field(default_factory=ProjectFields)
    parties: list[PartyFields] | None = None
    people: list[PersonFields] = Field(default_factory=list)


PARTY_COLUMNS = ('name', 'inn', 'kpp', 'ogrn', 'address', 'phone', 'email', 'sro')
PERSON_COLUMNS = ('full_name', 'position', 'email', 'phone', 'nrs_id')
APPOINTMENT_COLUMNS = ('role', 'order_number', 'order_date', 'valid_from', 'valid_to')
INACTIVE = ('cancelled', 'replaced', 'rejected', 'terminated', 'archived')
CONTRACT_ROLES = {
    ('general', 'contractor'): (Role.contractor, Role.developer),
    ('general', 'customer'): (Role.developer, Role.contractor),
    ('subcontract', 'customer'): (Role.contractor, Role.subcontractor),
    ('subcontract', 'contractor'): (Role.subcontractor, Role.contractor),
    ('supply', 'customer'): (None, Role.supplier),
    ('design', 'customer'): (None, Role.designer),
}


def _contract_roles(contract, counterparty=None):
    # Older subcontract records defaulted to "contractor" even for registered subcontractors.
    if contract.kind == 'subcontract' and counterparty and counterparty.kind == 'subcontractor':
        return CONTRACT_ROLES['subcontract', 'customer']
    return CONTRACT_ROLES.get((contract.kind, contract.our_role), (None, Role.other))


def _key(role, counterparty_id=None, *, own=False):
    return f'own:{role}' if own else f'party:{counterparty_id}:{role}'


def _fields(row, columns):
    return {field: getattr(row, field) for field in columns}


def _project_contracts(db, project):
    return list(db.scalars(select(Contract).where(
        Contract.organization_id == project.organization_id, Contract.object_id == project.id,
        Contract.status.notin_(INACTIVE)).order_by(Contract.number, Contract.id)))


def _counterparties(db, project):
    return {cp.id: cp for cp in db.scalars(select(Counterparty).where(
        Counterparty.organization_id == project.organization_id))}


def _meta(project):
    return (project.extra or {}).get('participant_structure') or {}


def _party(row, role):
    own = isinstance(row, Organization)
    signer = ((row.settings or {}).get('default_signer') or {}) if own else {}
    values = _fields(row, PARTY_COLUMNS)
    values.update(head_name=signer.get('name') if own else row.head_name,
                  head_position=signer.get('position') if own else row.head_position)
    return dict(key=_key(role, row.id, own=own), own=own, role=role,
                counterparty_id=None if own else str(row.id), **values,
                department=None, sources=[], contracts=[])


def registered_parties(db, project, *, counterparties=None, contracts=None):
    """Resolve project roles from active cards and contracts, without reading draft suggestions."""
    org = db.get(Organization, project.organization_id)
    contracts = _project_contracts(db, project) if contracts is None else contracts
    meta = _meta(project)
    if counterparties is None:
        ids = {project.developer_id, project.designer_id, *(c.counterparty_id for c in contracts)}
        ids.update(uuid.UUID(link['counterparty_id']) for link in meta.get('links', []) if link.get('counterparty_id'))
        counterparties = {cp.id: cp for cp in db.scalars(select(Counterparty).where(
            Counterparty.id.in_(ids - {None}), Counterparty.organization_id == project.organization_id))}
    parties = {}

    def add(role, row):
        if role and row is not None:
            key = _key(role, row.id, own=isinstance(row, Organization))
            if key not in parties:
                parties[key] = _party(row, role)
            return parties[key]
        return None

    add(Role.organization, org)
    add(Role.developer, counterparties.get(project.developer_id))
    add(Role.designer, counterparties.get(project.designer_id))
    for link in meta.get('links', []):
        cp_id = link.get('counterparty_id')
        row = org if link.get('own') else counterparties.get(uuid.UUID(cp_id)) if cp_id else None
        party = add(Role(link['role']), row)
        if party:
            party['department'] = link.get('department')

    for contract in contracts:
        cp = counterparties.get(contract.counterparty_id)
        own_role, other_role = _contract_roles(contract, cp)
        own = add(own_role, org)
        other = add(other_role, cp)
        for party in (own, other):
            if party:
                party['contracts'].append(dict(id=str(contract.id), number=contract.number, subject=contract.subject,
                                               document_id=str(contract.document_id) if contract.document_id else None))
    source_links = meta.get('contract_documents', {})
    source_ids = {uuid.UUID(doc_id) for ids in source_links.values() for doc_id in ids}
    if source_ids:
        scope = SourceContext(project.organization_id, project.id, False)
        documents = {str(doc.id): doc for doc in db.scalars(select(Document).where(
            Document.id.in_(source_ids), document_predicate(scope), Document.object_id == project.id,
            Document.kind == 'contract', Document.status.notin_(INACTIVE)))}
        for key, doc_ids in source_links.items():
            if key in parties:
                parties[key]['sources'] = [_source(documents[doc_id]) for doc_id in doc_ids if doc_id in documents]
    return list(parties.values())


def _matching_party(rows, values):
    inn = values.get('inn')
    name = (values.get('name') or '').strip().casefold()
    if inn:
        matches = [row for row in rows if row.inn == inn]
        if matches:
            return matches[0] if len(matches) == 1 else None
    matches = [row for row in rows if name and row.name.strip().casefold() == name
               and (not inn or not row.inn or row.inn == inn)]
    return matches[0] if len(matches) == 1 else None


def _source(doc, is_org=False):
    return dict(document_id=str(doc.id), title=doc.title, kind=doc.kind,
                level='organization' if is_org else 'project')


def _source_documents(db, project):
    scope = SourceContext(project.organization_id, project.id, False)
    org_docs = organization_documents(scope)
    return db.execute(select(Document, or_(Document.id.in_(org_docs), Document.object_id.is_(None)).label('is_org'))
        .where(document_predicate(scope), Document.source != 'generated', Document.status.notin_(INACTIVE),
               Document.kind.in_(['contract', 'order', 'project_doc', 'administrative', 'other']))
        .order_by(Document.id)).all()


def _label(text, labels, limit=300):
    match = re.search(rf'^[ \t]*(?:{labels})[ \t]*:[ \t]*([^\n|]+)', text, re.I | re.M)
    return match[1].strip()[:limit] if match else None


def _organization_card(text):
    return {
        'role': 'организация', 'kind': 'organization',
        'name': _label(text, r'полное наименование(?: организации)?|наименование организации|организация', 500),
        'inn': _label(text, r'ИНН', 12), 'kpp': _label(text, r'КПП', 9), 'ogrn': _label(text, r'ОГРН(?:ИП)?', 15),
        'address': _label(text, r'юридический адрес', 2000), 'phone': _label(text, r'телефон|тел\.', 100),
        'email': _label(text, r'email|e-mail|электронная почта', 200), 'sro': _label(text, r'СРО', 2000),
        'head_name': _label(text, r'ФИО подписанта|подписант'), 'head_position': _label(text, r'должность подписанта'),
    }


def _order_person(text, doc, parties):
    name = _label(text, r'ответственное лицо|ответственный|представитель|ФИО')
    if not name or not re.fullmatch(r'[А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё.-]*){1,2}', name):
        return None
    party_name = _label(text, r'организация|работодатель')
    candidates = [p for p in parties if party_name and (p.get('name') or '').casefold() == party_name.casefold()]
    identities = {(p['own'], p['counterparty_id']) for p in candidates}
    party = candidates[0] if len(identities) == 1 else None
    role = _label(text, r'роль|роль в ИД', 100)
    role = next((key for key, item in REPRESENTATIVE_ROLES.items()
                 if role and role.casefold() in {key, item['title'].casefold()}), None)
    return PersonFields(full_name=name, role=role, party_key=party['key'] if party else None,
                        department=_label(text, r'отдел|подразделение'), position=_label(text, r'должность'),
                        email=_label(text, r'email|e-mail|электронная почта', 200),
                        phone=_label(text, r'телефон|тел\.', 100), nrs_id=_label(text, r'НРС|номер НРС', 50),
                        order_number=doc.number or _label(text, r'номер приказа', 100),
                        order_date=doc.date).model_dump(mode='json')


def _appointments(db, project):
    return list(db.scalars(select(Appointment).where(
        Appointment.organization_id == project.organization_id, Appointment.object_id == project.id)
        .order_by(Appointment.id)))


def registered_people(db, project, parties, *, on_date=None):
    meta = _meta(project)
    party_keys = {p['counterparty_id']: p['key'] for p in parties if p['counterparty_id']}
    keys = {p['key'] for p in parties}
    people = []
    for ap in _appointments(db, project):
        if not ap.person or ap.person.organization_id != project.organization_id:
            continue
        if on_date and ((ap.valid_from and ap.valid_from > on_date) or (ap.valid_to and ap.valid_to < on_date)):
            continue
        info = meta.get('people', {}).get(str(ap.id), {})
        key = info.get('party_key')
        if key not in keys:
            key = party_keys.get(str(ap.person.counterparty_id)) if ap.person.counterparty_id else _key(Role.organization, own=True)
        row = PersonFields(appointment_id=ap.id, person_id=ap.person_id, party_key=key, department=info.get('department'),
                           **_fields(ap.person, PERSON_COLUMNS), **_fields(ap, APPOINTMENT_COLUMNS))
        people.append(row.model_dump(mode='json'))
    person_ids = {uuid.UUID(row['person_id']) for row in people}
    responsible_ids = {c.responsible_id for c in _project_contracts(db, project) if c.responsible_id} - person_ids
    if responsible_ids:
        rows = db.scalars(select(Person).where(Person.id.in_(responsible_ids),
                         Person.organization_id == project.organization_id).order_by(Person.id))
        for person in rows:
            key = party_keys.get(str(person.counterparty_id)) if person.counterparty_id else _key(Role.organization, own=True)
            people.append(PersonFields(person_id=person.id, party_key=key,
                **_fields(person, PERSON_COLUMNS)).model_dump(mode='json'))
    return people


def proposal(db, project):
    org = db.get(Organization, project.organization_id)
    counterparties = _counterparties(db, project)
    contracts = _project_contracts(db, project)
    parties = registered_parties(db, project, counterparties=counterparties, contracts=contracts)
    meta = _meta(project)
    by_key = {p['key']: p for p in parties}
    for saved in meta.get('partial_parties', []):
        by_key.setdefault(saved['key'], {**saved, 'sources': [], 'contracts': []})
    baseline = {key: dict(party) for key, party in by_key.items()}
    proposed = defaultdict(lambda: defaultdict(set))
    project_fields = ProjectFields(**_fields(project, tuple(ProjectFields.model_fields))).model_dump(mode='json')
    project_values = defaultdict(set)
    excluded = {key: set(ids) for key, ids in meta.get('excluded_sources', {}).items()}
    empty_fields = meta.get('empty_fields', {})
    project_sources = []
    people_found = []
    contracts_by_doc = {c.document_id: c for c in contracts if c.document_id}
    for doc, is_org in _source_documents(db, project):
        chunks = document_chunks(db, doc, include_vision=False)
        text = '\n'.join(hit.content for hit in chunks)
        if not text.strip():
            continue
        source = _source(doc, is_org)
        if doc.kind == 'order' and not is_org:
            people_found.append((doc, text, source))
        if doc.kind == 'project_doc' and not is_org:
            for field, labels in (('code', 'шифр проекта'), ('address', 'адрес объекта')):
                if value := _label(text, labels, 100 if field == 'code' else 2000):
                    project_values[field].add(value)
                    if source not in project_sources:
                        project_sources.append(source)
        if doc.kind != 'contract' and not is_org:
            continue
        extracted = parse_parties(text)
        if is_org and re.search(r'карточк[аи]\s+(?:организац|предприяти)|реквизиты организации', doc.title, re.I):
            extracted.append(_organization_card(text))
        for values in extracted:
            if not (values.get('name') or values.get('inn')):
                continue
            own = bool(_matching_party([org], values))
            if is_org and not own:
                continue
            kind = (doc.data or {}).get('contract_kind') or contract_kind(doc.title + '\n' + text)
            contract = contracts_by_doc.get(doc.id)
            role = Role(values['kind']) if values['kind'] in Role._value2member_map_ else Role.other
            if is_org:
                role = Role.organization
            elif contract:
                known = counterparties.get(contract.counterparty_id)
                roles = _contract_roles(contract, known)
                if (own or known is None or _matching_party([known], values)):
                    role = roles[0 if own else 1] or role
            elif kind == 'subcontract' and role == Role.developer:
                role = Role.contractor
            elif kind == 'subcontract' and values['role'] in {'подрядчик', 'исполнитель'}:
                role = Role.subcontractor
            row = org if own else _matching_party(counterparties.values(), values)
            identity = (values['inn'] or values['name'].strip().casefold()) if not row else None
            key = _key(role, row.id, own=own) if row else f'document:{role}:{identity}'
            if str(doc.id) in excluded.get(key, ()):
                continue
            if key not in by_key:
                by_key[key] = _party(row, role) if row else dict(key=key, role=role, own=False,
                    counterparty_id=None, name=None, sources=[], contracts=[])
                baseline[key] = dict(by_key[key])
            party = by_key[key]
            if source not in party['sources']:
                party['sources'].append(source)
            for field in (*PARTY_COLUMNS, 'head_name', 'head_position'):
                if values.get(field):
                    proposed[key][field].add(values[field])
    warnings = []
    for key, fields in proposed.items():
        for field, values in fields.items():
            if baseline[key].get(field) or field in empty_fields.get(key, ()):
                continue
            if len(values) == 1:
                by_key[key][field] = next(iter(values))
            else:
                warnings.append(f'В источниках расходятся сведения об участнике «{by_key[key].get("name") or key}».')
    for field, values in project_values.items():
        if not project_fields[field] and len(values) == 1 and field not in empty_fields.get('project', ()):
            project_fields[field] = next(iter(values))
    for role, label in ((Role.developer, 'заказчика'), (Role.contractor, 'генподрядчика'), (Role.designer, 'проектировщика')):
        if sum(p['role'] == role and bool(p.get('name')) for p in by_key.values()) > 1:
            warnings.append(f'Для роли {label} найдено несколько организаций. Проверьте роли в схеме.')
    people = registered_people(db, project, by_key.values())
    people.extend(meta.get('partial_people', []))
    for doc, content, source in people_found:
        row = _order_person(content, doc, by_key.values())
        if row and not any(r.get('full_name') == row['full_name'] and r.get('order_number') == row['order_number']
                          for r in people):
            people.append({**row, 'sources': [source]})
    for person in people:
        person.setdefault('sources', [])

    result = dict(project=project_fields, project_sources=project_sources, parties=list(by_key.values()), people=people,
                  warnings=list(dict.fromkeys(warnings)), confirmed_at=meta.get('confirmed_at'))
    result['revision'] = hashlib.sha256(json.dumps([str(project.id), result], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


class StaleStructure(ValueError):
    pass


def _apply_fields(row, fields):
    for key, value in fields.items():
        if key in {'name', 'full_name'} and not value:
            continue
        setattr(row, key, value if not isinstance(value, str) else value or None)


def _save_parties(db, project, org, submitted, meta, *, edit_org):
    counterparts = _counterparties(db, project)
    baseline = {cp.id: _fields(cp, (*PARTY_COLUMNS, 'head_name', 'head_position')) for cp in counterparts.values()}
    links = {}
    references = {}
    partial = []
    for index, party in enumerate(submitted):
        values = party.model_dump(exclude_unset=True, include=set(PARTY_COLUMNS) | {'head_name', 'head_position'})
        key = party.key or f'manual:{index}'
        if key in references:
            raise ValueError('Организация повторяется в схеме. Обновите страницу.')
        if party.role == Role.organization and not party.own:
            raise ValueError('Роль «Наша организация» предназначена для карточки своей организации')
        if party.own:
            if party.counterparty_id:
                raise ValueError('Неверная ссылка на свою организацию')
            if edit_org and party.role == Role.organization:
                old_signer = (org.settings or {}).get('default_signer') or {}
                signer = {**old_signer}
                for field, attribute in (('head_name', 'name'), ('head_position', 'position')):
                    if field in values:
                        signer[attribute] = values.pop(field) or None
                if signer != old_signer:
                    org.settings = {**(org.settings or {}), 'default_signer': signer}
                _apply_fields(org, values)
            row = org
        else:
            row = counterparts.get(party.counterparty_id) if party.counterparty_id else _matching_party(counterparts.values(), values)
            if party.counterparty_id and row is None:
                raise ValueError('Карточка организации недоступна')
            if row is None and party.name:
                row = Counterparty(organization_id=org.id, name=party.name, kind=party.role.value)
                db.add(row)
                db.flush()
                counterparts[row.id] = row
            if row is not None:
                original = baseline.get(row.id, {})
                _apply_fields(row, {field: value for field, value in values.items() if original.get(field) != value})
        if row is None:
            if any(values.values()) or party.department:
                partial.append({**party.model_dump(mode='json'), 'key': key})
            references[key] = (None, None)
            continue
        cp_id = None if party.own else row.id
        active_key = _key(party.role, cp_id, own=party.own)
        references[key] = (active_key, cp_id)
        links[active_key] = dict(role=party.role.value, own=party.own,
                                 counterparty_id=str(cp_id) if cp_id else None, department=party.department or None)
    for role, attr in ((Role.developer, 'developer_id'), (Role.designer, 'designer_id')):
        found = [p['counterparty_id'] for p in links.values() if p['role'] == role]
        existing = getattr(project, attr)
        if len(found) == 1:
            setattr(project, attr, uuid.UUID(found[0]) if found[0] else None)
        elif str(existing) not in found:
            setattr(project, attr, None)
    meta.update(links=list(links.values()), partial_parties=partial)
    return references


def _save_people(db, project, submitted, meta, references):
    known = {ap.id: ap for ap in _appointments(db, project)}
    persons = list(db.scalars(select(Person).where(Person.organization_id == project.organization_id)))
    info = dict(meta.get('people', {}))
    partial = []
    for person in submitted:
        existing = known.get(person.appointment_id)
        if person.appointment_id and existing is None:
            raise ValueError('Назначение недоступно для выбранного проекта')
        if person.valid_to and person.valid_from and person.valid_to < person.valid_from:
            raise ValueError('Дата окончания полномочий раньше даты начала')
        card = existing.person if existing else None
        if not card and person.person_id:
            card = next((p for p in persons if p.id == person.person_id), None)
            if card is None:
                raise ValueError('Карточка ответственного лица недоступна')
        party_key, cp_id = references.get(person.party_key, (None, card.counterparty_id if card else None))
        if card and card.organization_id != project.organization_id:
            raise ValueError('Карточка ответственного лица недоступна')
        values = person.model_dump(exclude_unset=True)
        if not card and not party_key:
            if any(values.get(key) for key in (*PERSON_COLUMNS, *APPOINTMENT_COLUMNS, 'department')):
                partial.append(person.model_dump(mode='json'))
            continue
        if not card or cp_id != card.counterparty_id:
            name = person.full_name or (card.full_name if card else None)
            match = [p for p in persons if name and p.full_name.casefold() == name.casefold() and p.counterparty_id == cp_id]
            if len(match) > 1:
                raise ValueError(f'По имени «{name}» найдено несколько карточек ответственных')
            card = match[0] if match else None
            if card is None and name:
                card = Person(organization_id=project.organization_id, full_name=name, counterparty_id=cp_id)
                db.add(card)
                db.flush()
                persons.append(card)
        if not card:
            if any(values.get(key) for key in (*PERSON_COLUMNS, *APPOINTMENT_COLUMNS, 'department')):
                partial.append({**person.model_dump(mode='json'), 'party_key': party_key or person.party_key})
            continue
        _apply_fields(card, {key: values[key] for key in PERSON_COLUMNS if key in values})
        role = person.role or 'responsible'
        if not existing and person.person_id and not person.role and not any(
                values.get(key) for key in ('order_number', 'order_date', 'valid_from', 'valid_to', 'department')):
            continue  # Contract responsibility does not imply an appointment to sign acts.
        if not existing:
            matches = [ap for ap in known.values() if ap.person_id == card.id and ap.role == role
                       and ap.order_number == person.order_number and ap.valid_from == person.valid_from
                       and ap.valid_to == person.valid_to]
            existing = matches[0] if len(matches) == 1 else None
        if not existing:
            existing = Appointment(organization_id=project.organization_id, object_id=project.id, person_id=card.id, role=role)
            db.add(existing)
        existing.person = card
        applied = {key: values[key] for key in APPOINTMENT_COLUMNS if key in values}
        if 'role' in applied:
            applied['role'] = role
        _apply_fields(existing, applied)
        db.flush()
        known[existing.id] = existing
        info[str(existing.id)] = dict(party_key=party_key, department=person.department or None)
    meta.update(people=info, partial_people=partial)


def confirm(db, project, body, user, *, edit_org):
    org = db.scalar(select(Organization).where(Organization.id == project.organization_id).with_for_update()
                    .execution_options(populate_existing=True))
    project = db.scalar(select(ConstructionObject).where(
        ConstructionObject.id == project.id, ConstructionObject.organization_id == org.id).with_for_update()
        .execution_options(populate_existing=True))
    if project is None:
        raise StaleStructure('Проект удалён. Обновите список проектов.')
    current = proposal(db, project)
    if body.revision and body.revision != current['revision']:
        raise StaleStructure('Карточки или документы изменились. Обновите схему и проверьте данные ещё раз.')
    meta = dict(_meta(project))
    empty_fields = {key: set(fields) for key, fields in meta.get('empty_fields', {}).items()}

    def remember_blanks(key, submitted, original):
        blanks = empty_fields.setdefault(key, set())
        for field, value in submitted.items():
            if value:
                blanks.discard(field)
            elif original.get(field):
                blanks.add(field)

    project_fields = body.project.model_dump(exclude_unset=True)
    remember_blanks('project', project_fields, current['project'])
    _apply_fields(project, project_fields)
    if body.parties is None:
        references = {p['key']: (p['key'], uuid.UUID(p['counterparty_id']) if p['counterparty_id'] else None)
                      for p in registered_parties(db, project)}
    else:
        references = _save_parties(db, project, org, body.parties, meta, edit_org=edit_org)
        originals = {p['key']: p for p in current['parties']}
        excluded = {key: set(ids) for key, ids in meta.get('excluded_sources', {}).items()}
        for key, original in originals.items():
            if key not in references and original['role'] != Role.organization and not original['contracts']:
                excluded.setdefault(key, set()).update(source['document_id'] for source in original['sources'])
        source_links = defaultdict(set)
        for party in body.parties:
            if party.key and party.key in originals:
                active_key, cp_id = references[party.key]
                if active_key:
                    source_links[active_key].update(source['document_id'] for source in originals[party.key]['sources']
                                                    if source['kind'] == 'contract' and source['level'] == 'project')
                if active_key and (not party.own or edit_org):
                    values = party.model_dump(exclude_unset=True, include=set(PARTY_COLUMNS) | {'head_name', 'head_position'})
                    remember_blanks(active_key, values, originals[party.key])
                old_role = originals[party.key]['role']
                if active_key and party.role != old_role:
                    ids = {source['document_id'] for source in originals[party.key]['sources']}
                    excluded.setdefault(party.key, set()).update(ids)
                    excluded.setdefault(_key(old_role, cp_id, own=party.own), set()).update(ids)
        meta['excluded_sources'] = {key: sorted(ids) for key, ids in excluded.items() if ids}
        meta['contract_documents'] = {key: sorted(ids) for key, ids in source_links.items() if ids}
        for contract in _project_contracts(db, project):
            if not contract.document_id or contract.counterparty_id:
                continue
            _, role = _contract_roles(contract)
            doc_id = str(contract.document_id)
            matches = set()
            for party in body.parties:
                active_key, cp_id = references.get(party.key, (None, None))
                if cp_id and party.role == role and doc_id in source_links[active_key]:
                    matches.add(cp_id)
            if len(matches) == 1:
                contract.counterparty_id = matches.pop()
    meta['empty_fields'] = {key: sorted(fields) for key, fields in empty_fields.items() if fields}
    if 'people' in body.model_fields_set:
        _save_people(db, project, body.people, meta, references)
    meta['confirmed_at'] = dt.datetime.now(dt.UTC).isoformat()
    project.extra = {**(project.extra or {}), 'participant_structure': meta}
    audit(db, org.id, actor_type='user', actor=str(user.id), action='project.structure.confirm',
          entity_type='object', entity_id=project.id)
    db.flush()
    return project


def recipient(db, project, *, role=None, counterparty_id=None, contract=None):
    """Return an address only when the active registries identify a single candidate."""
    if contract and contract.object_id and project and contract.object_id != project.id:
        raise ValueError('Договор относится к другому проекту')
    if contract:
        if not contract.counterparty_id or (counterparty_id and counterparty_id != contract.counterparty_id):
            return None
        cp = db.get(Counterparty, contract.counterparty_id)
        return _party(cp, Role.other) if cp and cp.organization_id == contract.organization_id else None
    if not project or not (role or counterparty_id):
        return None
    candidates = {p['counterparty_id']: p for p in registered_parties(db, project)
                  if p['counterparty_id'] and (not role or p['role'] == role)
                  and (not counterparty_id or p['counterparty_id'] == str(counterparty_id))}
    return next(iter(candidates.values())) if len(candidates) == 1 else None
