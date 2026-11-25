"""Resolve reviewed template bindings from scoped BIS registries and grounded project facts."""
from __future__ import annotations

import datetime as dt
import hashlib
from collections import defaultdict
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import Date, String, and_, cast, or_, select

from bis.documents.aosr import REPRESENTATIVE_ROLES, Representative
from bis.documents.common import Party, fmt_date, short_name
from bis.models import (
    ConstructionObject,
    Contract,
    Document,
    IntakeEvent,
    KnowledgeChunk,
    KnowledgeSource,
    Organization,
    WorkVolume,
)
from bis.models.facts import FactConflict, FactDecision, FactSnapshot, ProjectFact
from bis.models.libraries import LibraryVersion
from bis.services import project_facts, project_participants
from bis.services.library_access import bind_context, document_predicate

PARTIES = {
    'org': 'Наша организация', 'developer': 'Заказчик', 'builder': 'Генподрядчик',
    'designer': 'Проектировщик', 'subcontractor': 'Субподрядчик', 'performer': 'Исполнитель работ',
}
PARTY_DETAILS = {
    'name': 'наименование', 'inn': 'ИНН', 'kpp': 'КПП', 'ogrn': 'ОГРН', 'address': 'адрес',
    'phone': 'телефон', 'email': 'электронная почта', 'sro': 'СРО', 'head_name': 'ФИО руководителя',
    'head_position': 'должность руководителя', 'text': 'реквизиты',
}
SOURCE_FIELD_LABELS = {
    **{f'{prefix}_{attr}': f'{label} — {description}' for prefix, label in PARTIES.items()
       for attr, description in PARTY_DETAILS.items()},
    'org_short_name': 'Сокращённое наименование нашей организации',
    'object_address': 'Адрес объекта', 'object_code': 'Шифр объекта',
    'contract_number': 'Номер договора', 'contract_date': 'Дата договора', 'contract_subject': 'Предмет договора',
    'quantity': 'Фактический объём работ', 'unit': 'Единица измерения', 'work_date': 'Дата выполнения работ',
    'certificate_number': 'Номер документа о качестве', 'certificate_expiry': 'Срок действия документа о качестве',
    'batch': 'Партия материала', 'protocol_result': 'Результат испытаний',
}
FACT_PREDICATES = {
    'works': ('work', 'actual'), 'materials': ('material', 'actual'),
    'work_start': ('work_start', 'actual'), 'work_end': ('work_end', 'actual'),
    'work_date': ('work_date', 'actual'), 'batch': ('batch', 'actual'),
    'quantity': ('quantity', 'actual'), 'unit': ('unit', 'actual'),
    'project_docs': ('project_ref', None), 'norms': ('norm', 'requirement'),
    'next_works': ('next_work', 'requirement'), 'contract_number': ('contract_number', None),
    'certificate_number': ('certificate_number', None), 'certificate_expiry': ('certificate_expiry', None),
    'protocol_result': ('protocol_result', 'result'),
}
DOCUMENT_FIELDS = {'executive_docs': {'scheme', 'test_protocol'}, 'tests': {'test_protocol'}, 'aosr_refs': {'aosr'}}
REP_PARTY = {
    'developer_control': 'developer', 'builder': 'builder', 'builder_control': 'builder',
    'designer': 'designer', 'performer': 'performer',
}
DATE_FACTS = {'work_start', 'work_end', 'work_date', 'certificate_expiry'}
TEXT_FIELDS = {
    'work_journal': {'works', 'materials', 'work_start', 'work_end', 'batch', 'quantity', 'unit', 'project_docs'},
    'project_doc': {'project_docs', 'norms', 'next_works'},
    'certificate': {'certificate_number', 'certificate_expiry'}, 'contract': {'contract_number'},
    'test_protocol': {'protocol_result'},
}
CANDIDATE_LIMIT = 1000


def _candidate(identifier, kind, label, value, **evidence):
    if isinstance(value, dt.date):
        value = fmt_date(value)
    if value is not None and (value := str(value).strip()):
        return {'id': identifier, 'kind': kind, 'label': label, 'value': value,
                **{field: item for field, item in evidence.items() if item is not None}}
    return None


def _text_parts(*values):
    return ', '.join(str(value) for value in values if value)


def _add(index, field, identifier, kind, label, value, **evidence):
    if (item := _candidate(identifier, kind, label, value, **evidence)) is not None:
        index[field].append(item)


def _party_candidates(index, party, prefix):
    label = f"Карточка участника · {PARTIES[prefix]} · {party['name']}"
    identifier = f"registry:{party['key']}"
    for attr in PARTY_DETAILS:
        value = Party.model_validate(party).format() if attr == 'text' else party.get(attr)
        _add(index, f'{prefix}_{attr}', identifier, 'registry', label, value)


def _identity(party):
    return party['own'], party['counterparty_id']


def _party_roles(parties, people, project):
    by_role = defaultdict(list)
    prefixes = {'organization': 'org', 'developer': 'developer', 'contractor': 'builder',
                'designer': 'designer', 'subcontractor': 'subcontractor'}
    for p in parties:
        if p['role'] in prefixes and p['name']:
            by_role[prefixes[p['role']]].append(p)
    for role, key in (('developer', project.developer_id), ('designer', project.designer_id)):
        if key:
            selected = [p for p in by_role[role] if p['counterparty_id'] == str(key)]
            if selected:
                by_role[role] = selected
    by_key = {p['key']: p for p in parties}
    performer_ids = {_identity(p) for row in people if row['appointment_id'] and row['role'] == 'performer'
                     and (p := by_key.get(row['party_key']))}
    by_role['performer'] = [p for p in by_role['subcontractor'] if _identity(p) in performer_ids]
    if not by_role['performer'] and len(by_role['builder']) == 1:
        # Only a matching active appointment can identify a contractor as executor.
        by_role['performer'] = [p for p in by_role['builder'] if _identity(p) in performer_ids]
    return by_role, by_key


def _representatives(index, people, roles, by_key):
    groups = defaultdict(dict)
    others = {}
    for row in people:
        if not row['appointment_id'] or not row['full_name'] or not row['party_key']:
            continue
        party = by_key.get(row['party_key'])
        if not party:
            continue
        role = row['role']
        expected = REP_PARTY.get(role)
        if expected and not any(_identity(p) == _identity(party) for p in roles[expected]):
            continue
        order = row['order_number']
        if order:
            order = f"Приказ о назначении № {order}"
            if row['order_date']:
                order += f" от {fmt_date(dt.date.fromisoformat(row['order_date']))}"
        label = _text_parts(order or "Реестр назначений по проекту", party['name'])
        rep = Representative(full_name=row['full_name'], position=row['position'] or '',
                             nrs_id=row['nrs_id'], order=order, organization=party['name'])
        identifier = f"appointment:{row['appointment_id']}"
        if role not in REPRESENTATIVE_ROLES:
            others[identifier] = (rep, label, identifier)
            continue
        # Different active orders can carry different powers, even for one person.
        groups[role][identifier] = (rep, label)
        _add(index, f'representative_{role}', identifier, 'registry', label,
             rep.format(with_nrs=REPRESENTATIVE_ROLES[role]['nrs']))
        _add(index, f'signature_{role}', identifier, 'registry', label,
             ' '.join(filter(None, (rep.position, short_name(rep.full_name)))))
    present = {role for role, rows in groups.items() if len(rows) == 1}
    required = {role for role, meta in REPRESENTATIVE_ROLES.items() if meta['required']}
    # An aggregate slot cannot represent selecting several conflicting signers independently.
    # Require individual bindings or reviewed text rather than emitting a partial block.
    ambiguous = set(groups) - present
    if required <= present and not ambiguous:
        evidence, lines = [], []
        for role in REPRESENTATIVE_ROLES:
            if role not in present:
                continue
            identifier, (rep, label) = next(iter(groups[role].items()))
            lines.append(f"{REPRESENTATIVE_ROLES[role]['title']}: {rep.format(with_nrs=REPRESENTATIVE_ROLES[role]['nrs'])}")
            evidence.append({'id': identifier, 'label': label})
        token = hashlib.sha256('|'.join(row['id'] for row in evidence).encode()).hexdigest()
        _add(index, 'representatives', f'appointments:{token}', 'registry', 'Реестр назначений по проекту',
             '\n'.join(lines), evidence=evidence)
    if others:
        evidence = [{'id': identifier, 'label': label} for _, label, identifier in others.values()]
        token = hashlib.sha256('|'.join(row['id'] for row in evidence).encode()).hexdigest()
        _add(index, 'other_representatives', f'appointments:{token}', 'registry', 'Реестр иных представителей',
             '\n'.join(rep.format() for rep, _, _ in others.values()), evidence=evidence)
    return ambiguous, required - present


def _active_contracts(db, scope, when):
    return db.execute(select(Contract, Document.id).outerjoin(Document, and_(
        Document.id == Contract.document_id, document_predicate(scope), Document.status.notin_(project_participants.INACTIVE)))
        .where(Contract.organization_id == scope.organization_id, Contract.object_id == scope.object_id,
               Contract.status.notin_(project_participants.INACTIVE),
               or_(Contract.date.is_(None), Contract.date <= when),
               or_(Contract.start_date.is_(None), Contract.start_date <= when),
               or_(Contract.end_date.is_(None), Contract.end_date >= when)).order_by(Contract.id)).all()


def _contract_candidates(index, rows):
    for contract, document_id in rows:
        label = f"Карточка договора № {contract.number}"
        for attr in ('number', 'date', 'subject'):
            _add(index, f'contract_{attr}', f'contract:{contract.id}', 'registry', label,
                 getattr(contract, attr), document_id=str(document_id) if document_id else None)


def _registry(db, scope, project, when, wanted):
    index = defaultdict(list)
    org = db.get(Organization, scope.organization_id)
    label = f'Карточка нашей организации · {org.name}'
    org_party = {attr: getattr(org, attr, None) for attr in PARTY_DETAILS}
    signer = (org.settings or {}).get('default_signer') or {}
    org_party.update(key=f'organization:{org.id}', head_name=signer.get('name'), head_position=signer.get('position'))
    _party_candidates(index, org_party, 'org')
    _add(index, 'org_short_name', f'registry:organization:{org.id}', 'registry', label, org.short_name)
    _add(index, 'signer_name', f'registry:organization:{org.id}:signer', 'registry', label, signer.get('name'))
    _add(index, 'signer_position', f'registry:organization:{org.id}:signer', 'registry', label, signer.get('position'))
    index['sender_text'] = index['org_text']
    if not project:
        return index, set(), set()
    label = f'Карточка проекта · {project.name}'
    _add(index, 'object_name', f'object:{project.id}', 'registry', label, _text_parts(project.name, project.address))
    _add(index, 'object_address', f'object:{project.id}', 'registry', label, project.address)
    _add(index, 'object_code', f'object:{project.id}', 'registry', label, project.code)

    contracts = _active_contracts(db, scope, when)
    parties = project_participants.registered_parties(db, project, contracts=[row[0] for row in contracts])
    people = project_participants.registered_people(db, project, parties, on_date=when)
    people = [row for row in people if not row['order_date'] or dt.date.fromisoformat(row['order_date']) <= when]
    roles, by_key = _party_roles(parties, people, project)
    for role in PARTIES:
        if role != 'org':
            for party in roles[role]:
                _party_candidates(index, party, role)
    if 'addressee' in wanted:
        for p in parties:
            if not p['own'] and p['name']:
                _add(index, 'addressee', f"registry:{p['key']}", 'registry', f"Участник проекта · {p['name']}",
                     Party.model_validate(p).format())
    if wanted & {'contract_number', 'contract_date', 'contract_subject'}:
        _contract_candidates(index, contracts)
    ambiguous, missing = _representatives(index, people, roles, by_key)
    return index, ambiguous, missing


def _confirmed_decisions(db, scope, pairs):
    groups = defaultdict(set)
    eligible = {str(fact.id) for fact, _ in pairs}
    for fact, _ in pairs:
        groups[(fact.predicate, fact.location_key, fact.work_key)].add(str(fact.id))
    keep, remove = set(), set()
    rows = db.execute(select(FactConflict, FactDecision).join(FactDecision, and_(
        FactDecision.conflict_id == FactConflict.id, FactDecision.revision == FactConflict.revision,
        FactDecision.signature == FactConflict.signature)).where(
        FactConflict.organization_id == scope.organization_id, FactConflict.object_id == scope.object_id,
        FactDecision.organization_id == scope.organization_id, FactDecision.object_id == scope.object_id,
        FactConflict.kind == 'different_values', FactConflict.state == 'resolved'))
    for conflict, choice in rows:
        group = groups[(conflict.predicate, conflict.location_key, conflict.work_key)]
        chosen = str(choice.fact_id)
        if chosen in eligible and chosen in group and group == set(conflict.fact_ids):
            keep.add(chosen)
            remove.update(group - {chosen})
    return keep, remove - keep


def _grounded(fact, snap):
    if not fact.quote.strip() or not fact.value.strip():
        return False
    if snap.document_id:
        # The active fact ledger has already checked source quotes against the document.
        return True
    if snap.work_volume_id:
        return snap.details.get('status') in ('confirmed', 'closed')
    correction = {'material': 'materials'}.get(fact.predicate, fact.predicate)
    if snap.method == 'user_correction' and correction in snap.details.get('corrections', {}):
        return True
    # Data extracted from unapproved production messages must be grounded in the original wording.
    from bis.services.executive_pipeline import verified_fact

    return verified_fact(SimpleNamespace(field=fact.predicate, value=fact.value, quote=fact.quote,
                                         location=None, work=None, entity=None), snap.details.get('report_text') or '')


def _dated_value(predicate, value, when):
    if predicate not in DATE_FACTS:
        return value
    try:
        value = dt.date.fromisoformat(value)
    except ValueError:
        return None
    return value if predicate == 'certificate_expiry' or value <= when else None


def _indexed_text_candidates(index, db, ctx, project_id, when, target_specs, *, work, location):
    """Read exact-current project text via the existing rules until the ledger indexes the source."""
    from bis.services.executive_pipeline import heuristic_facts, verified_fact

    kinds = {kind for kind, fields in TEXT_FIELDS.items() if fields & target_specs.keys()}
    if not kinds:
        return
    known = select(FactSnapshot.id).where(
        FactSnapshot.document_id == Document.id, FactSnapshot.file_key.is_not_distinct_from(Document.file_key),
        FactSnapshot.kind == Document.kind, FactSnapshot.organization_id == ctx.org_id,
        FactSnapshot.object_id == project_id, FactSnapshot.state.in_(('active', 'partial'))).exists()
    linked_version = select(LibraryVersion.id).where(
        LibraryVersion.document_id == Document.id, LibraryVersion.source_id == KnowledgeSource.id,
        LibraryVersion.state == 'ready',
        cast(LibraryVersion.id, String) == Document.data['library_version_id'].astext).exists()
    docs = project_facts.source_documents(ctx, project_id).with_only_columns(Document.id)
    rows = db.execute(select(Document, KnowledgeChunk).join(KnowledgeSource, KnowledgeSource.document_id == Document.id)
        .join(KnowledgeChunk, KnowledgeChunk.source_id == KnowledgeSource.id).where(
            Document.id.in_(docs), Document.kind.in_(kinds), ~known, Document.source != 'generated',
            Document.status.notin_(project_participants.INACTIVE),
            or_(Document.date.is_(None), Document.date <= when),
            KnowledgeSource.organization_id == ctx.org_id, KnowledgeChunk.organization_id == ctx.org_id,
            KnowledgeSource.status == 'active', KnowledgeSource.meta['vision'].astext.is_distinct_from('true'),
            or_(and_(Document.file_key.is_not(None), Document.file_key == KnowledgeSource.file_key),
                Document.data['sha256'].astext == KnowledgeSource.meta['original_sha256'].astext, linked_version))
        .order_by(Document.id, KnowledgeSource.id, KnowledgeChunk.ord).limit(CANDIDATE_LIMIT + 1)).all()
    if len(rows) > CANDIDATE_LIMIT:
        raise ValueError('Слишком много исходного текста для заполнения. Сначала обновите реестр фактов проекта')
    for doc, chunk in rows:
        text = '\n'.join(line for line in chunk.content.splitlines() if len(line) <= 1500)
        for fact in heuristic_facts(text).facts:
            if (not verified_fact(fact, text)
                or (work and project_facts.work_key(work) != project_facts.work_key(fact.work or fact.quote))
                or (location and project_facts.location_key(location) != project_facts.location_key(fact.location or fact.quote))):
                continue
            value = _dated_value(fact.field, fact.value, when)
            token = project_facts.digest((chunk.id, fact.field, fact.value, fact.quote))
            for name in TEXT_FIELDS[doc.kind] & target_specs.keys():
                predicate, category = target_specs[name]
                if fact.field == predicate and (category is None or category == project_facts.category(doc.kind, predicate)):
                    _add(index, name, f'text:{token}', 'documents', f'{doc.title} · текст документа', value,
                         document_id=str(doc.id), page=chunk.page, quote=fact.quote)


def _fact_index(db, scope, when, wanted, *, work=None, location=None):
    index = defaultdict(list)
    target_specs = {name: spec for name, spec in FACT_PREDICATES.items() if name in wanted}
    doc_fields = {name: types for name, types in DOCUMENT_FIELDS.items() if name in wanted}
    if not scope.object_id or not (target_specs or doc_fields):
        return index
    predicates = {spec[0] for spec in target_specs.values()}
    kinds = set().union(*doc_fields.values()) if doc_fields else set()
    ctx = SimpleNamespace(db=db, org_id=scope.organization_id)
    ineligible_docs = select(Document.id).where(or_(Document.status.in_(project_participants.INACTIVE), Document.date > when))
    future_reports = select(IntakeEvent.id).where(cast(IntakeEvent.created_at, Date) > when)
    future_volumes = select(WorkVolume.id).where(WorkVolume.date > when)
    query = project_facts.current_query(ctx, scope.object_id).where(
        or_(ProjectFact.predicate.in_(predicates), FactSnapshot.kind.in_(kinds)),
        or_(FactSnapshot.document_id.is_(None), FactSnapshot.document_id.notin_(ineligible_docs)),
        or_(FactSnapshot.intake_event_id.is_(None), FactSnapshot.intake_event_id.notin_(future_reports)),
        or_(FactSnapshot.work_volume_id.is_(None), FactSnapshot.work_volume_id.notin_(future_volumes)))
    if location:
        query = query.where(ProjectFact.location_key == project_facts.location_key(location))
    if work:
        query = query.where(ProjectFact.work_key == project_facts.work_key(work))
    raw_rows = db.execute(query.order_by(ProjectFact.id).limit(CANDIDATE_LIMIT + 1)).all()
    if len(raw_rows) > CANDIDATE_LIMIT:
        raise ValueError('Найдено слишком много проектных фактов. Уточните место или вид работ')
    pairs = [(fact, snap) for fact, snap in raw_rows if _grounded(fact, snap)]
    chosen, rejected = _confirmed_decisions(db, scope, pairs)
    listed_docs = set()
    for fact, snap in pairs:
        if str(fact.id) in rejected:
            continue
        value = _dated_value(fact.predicate, fact.value, when)
        kind = 'documents' if snap.document_id else 'project'
        label = snap.title + (' · подтверждённый выбор' if str(fact.id) in chosen else '')
        evidence = {'document_id': str(snap.document_id) if snap.document_id else None,
                    'page': fact.page, 'quote': fact.quote,
                    'report_id': str(snap.intake_event_id) if snap.intake_event_id else None,
                    'work_volume_id': str(snap.work_volume_id) if snap.work_volume_id else None}
        for name, (predicate, category) in target_specs.items():
            if predicate == fact.predicate and (category is None or category == fact.category):
                _add(index, name, f'fact:{fact.id}', kind, label, value, **evidence)
        for name, types in doc_fields.items():
            if snap.document_id and snap.kind in types and (name, snap.document_id) not in listed_docs:
                _add(index, name, f'document:{snap.document_id}', 'documents', snap.title, snap.title, **evidence)
                listed_docs.add((name, snap.document_id))
    _indexed_text_candidates(index, db, ctx, scope.object_id, when, target_specs, work=work, location=location)
    return index


def _field_result(entry, candidates, manual, selections):
    target = entry.target
    if target in manual:
        value = str(manual[target]).strip()
        if value:
            sources = [_candidate(f'manual:{target}', 'manual', 'Введено пользователем', value)]
            return {'field': entry.field, 'state': 'resolved', 'value': value, 'sources': sources}
        candidates = []
    if target in selections:
        chosen = next((item for item in candidates if item['id'] == selections[target]), None)
        if chosen is None:
            raise ValueError('Источник изменился или недоступен для этого поля')
        candidates = [chosen]
    values = {item['value'] for item in candidates}
    state = 'resolved' if len(values) == 1 else 'conflict' if values else 'missing'
    return {'field': entry.field, 'state': state, 'value': next(iter(values)) if len(values) == 1 else None,
            'sources': candidates}


def resolve(db, user, mapping, *, object_id=None, act_date=None, values=None, selections=None,
            work=None, location=None, as_of=None):
    """Resolve per target so repeated fields may use different sources or explicit choices."""
    values, selections = values or {}, selections or {}
    targets = {entry.target for entry in mapping.entries}
    if (set(values) | set(selections)) - targets or set(values) & set(selections):
        raise ValueError('Источник или ручное значение не соответствует полям карты')

    prior = db.info.get('source_context')
    project_id = UUID(str(object_id)) if object_id else None
    if prior and prior.organization_id == user.organization_id and prior.object_id:
        if project_id and prior.object_id != project_id:
            raise LookupError('Проект недоступен в выбранном контексте')
        project_id = prior.object_id
    scope = bind_context(db, user.organization_id, project_id)
    try:
        when = act_date or as_of or dt.date.today()
        project = db.get(ConstructionObject, scope.object_id) if scope.object_id else None
        wanted = {entry.field for entry in mapping.entries
                  if (getattr(entry, 'source', None) or 'auto') in ('auto', 'project', 'documents')}
        registry, ambiguous, missing = _registry(db, scope, project, when, {e.field for e in mapping.entries})
        facts = _fact_index(db, scope, when, wanted, work=work, location=location)
        fields = {}
        for entry in mapping.entries:
            source = getattr(entry, 'source', None) or 'auto'
            items = [*registry.get(entry.field, []), *facts.get(entry.field, [])]
            if source != 'auto':
                items = [item for item in items if item['kind'] == source]
            if entry.field == 'date' and act_date and source in ('auto', 'manual'):
                items.append(_candidate('manual:act_date', 'manual', 'Дата документа', act_date))
            if entry.field == 'clear_example_value':
                if entry.target in values or entry.target in selections:
                    raise ValueError('Удаление примера не принимает значение или источник')
                fields[entry.target] = {'field': entry.field, 'state': 'resolved', 'value': '', 'sources': []}
                continue
            result = _field_result(entry, items, values, selections)
            if entry.field == 'representatives' and (ambiguous or missing) and result['state'] == 'missing':
                result['state'] = 'conflict' if ambiguous else 'missing'
                result['issues'] = [REPRESENTATIVE_ROLES[r]['title'] for r in sorted(ambiguous | missing)]
            fields[entry.target] = result
        return {'fields': fields}
    finally:
        if prior is None:
            db.info.pop('source_context', None)
        else:
            db.info['source_context'] = prior
