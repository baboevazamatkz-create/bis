"""Project fact ledger. A grounded assertion is evidence, not an unquestionable truth."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import Numeric, String, cast, or_, select, update

from bis.models import ConstructionObject, Document, EstimateItem, IntakeEvent, WorkVolume
from bis.models.facts import FactConflict, FactDecision, FactSnapshot, ProjectFact
from bis.services.library_access import SourceContext, bind_context, document_predicate

SCHEMA_VERSION = 1
class SourceInProgress(RuntimeError):
    pass


DOCUMENT_KINDS = ('project_doc', 'work_journal', 'certificate', 'test_protocol', 'scheme', 'aosr', 'aook', 'order', 'contract')
LABELS = {'material': 'Материал', 'project_ref': 'Рабочая документация', 'batch': 'Партия',
    'work_start': 'Начало работ', 'work_end': 'Окончание работ', 'quality_requirement': 'Документы качества',
    'protocol_requirement': 'Необходимые испытания', 'scheme_requirement': 'Исполнительная схема', 'act_kind': 'Вид акта', 'norm': 'Нормативное основание',
    'next_work': 'Последующие работы', 'work_date': 'Дата выполнения', 'work': 'Выполненная работа', 'quantity': 'Объём', 'unit': 'Единица измерения',
    'certificate_number': 'Номер документа качества', 'certificate_expiry': 'Срок действия документа качества',
    'protocol_result': 'Заключение испытаний', 'contract_number': 'Номер договора'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def scope(ctx, object_id=None):
    from bis.agents.resolve import obj_row

    project = obj_row(ctx, str(object_id) if object_id else None)
    current = ctx.db.info.get('source_context')
    if current and current.object_id and current.object_id != project.id:
        raise LookupError('Факты другого проекта недоступны в текущей задаче')
    bind_context(ctx.db, ctx.org_id, project.id)
    ctx.focus['object'] = str(project.id)
    return project.id


def work_key(text):
    from bis.services.executive_pipeline import work_keys

    keys = work_keys(text)
    if len(keys) > 1:
        keys.discard('монтаж')
    return '|'.join(sorted(keys))[:500]


def location_key(text):
    from bis.services.executive_pipeline import locations

    return '|'.join(sorted(locations(text or '')))[:500]


def category(kind, predicate):
    if predicate in ('quality_requirement', 'protocol_requirement', 'scheme_requirement', 'act_kind', 'norm', 'next_work'):
        return 'requirement'
    if kind in ('report', 'correction', 'work_journal', 'work_volume') and predicate in ('material', 'batch', 'work_start', 'work_end', 'work', 'work_date', 'quantity', 'unit'):
        return 'actual'
    if kind == 'project_doc' and predicate in ('material', 'quantity', 'unit', 'work'):
        return 'planned'
    return 'result' if predicate == 'protocol_result' else 'reference'


def normalized(predicate, value):
    from bis.services.executive_pipeline import material_key, norm

    return material_key(value) if predicate == 'material' else norm(value)


def source_documents(ctx, project_id):
    return select(Document).where(Document.organization_id == ctx.org_id, Document.object_id == project_id,
        Document.kind.in_(DOCUMENT_KINDS), Document.status.notin_(['cancelled', 'replaced', 'rejected']),
        Document.data['executive_run_id'].astext.is_(None),
        document_predicate(SourceContext(ctx.org_id, project_id, True)))


def current_query(ctx, project_id):
    docs = source_documents(ctx, project_id).with_only_columns(Document.id)
    document_snapshots = select(FactSnapshot.id).join(Document, Document.id == FactSnapshot.document_id).where(
        Document.id.in_(docs), FactSnapshot.file_key.is_not_distinct_from(Document.file_key), FactSnapshot.kind == Document.kind)
    reports = select(IntakeEvent.id).where(IntakeEvent.organization_id == ctx.org_id, IntakeEvent.object_id == project_id)
    volumes = select(FactSnapshot.id).join(WorkVolume, WorkVolume.id == FactSnapshot.work_volume_id).join(
        EstimateItem, EstimateItem.id == WorkVolume.estimate_item_id).where(
        WorkVolume.organization_id == ctx.org_id, WorkVolume.object_id == project_id,
        EstimateItem.organization_id == ctx.org_id, EstimateItem.object_id == project_id,
        cast(FactSnapshot.details['quantity'].astext, Numeric) == WorkVolume.quantity,
        FactSnapshot.details['date'].astext == cast(WorkVolume.date, String),
        FactSnapshot.details['work'].astext == EstimateItem.name,
        FactSnapshot.details['unit'].astext == EstimateItem.unit,
        FactSnapshot.details['location'].astext.is_not_distinct_from(WorkVolume.location),
        FactSnapshot.details['status'].astext == WorkVolume.status)
    return select(ProjectFact, FactSnapshot).join(FactSnapshot, FactSnapshot.id == ProjectFact.snapshot_id).where(
        ProjectFact.organization_id == ctx.org_id, ProjectFact.object_id == project_id,
        FactSnapshot.organization_id == ctx.org_id, FactSnapshot.object_id == project_id,
        FactSnapshot.state.in_(['active', 'partial']),
        or_(FactSnapshot.id.in_(document_snapshots), FactSnapshot.intake_event_id.in_(reports), FactSnapshot.id.in_(volumes)))


def invalidate_document(db, document_id):
    if document_id:
        db.execute(update(FactSnapshot).where(FactSnapshot.document_id == document_id,
            FactSnapshot.state.in_(['active', 'partial'])).values(state='stale'))


def _publish(ctx, *, project_id, origin_key, fingerprint, text_hash, kind, title, method, claims,
             document=None, event=None, volume=None, file_hash=None, details=None, pages=None, context_text=""):
    from bis.services.executive_pipeline import locations

    db = ctx.db
    old = db.scalar(select(FactSnapshot).where(FactSnapshot.organization_id == ctx.org_id,
        FactSnapshot.object_id == project_id, FactSnapshot.origin_key == origin_key, FactSnapshot.fingerprint == fingerprint))
    db.execute(update(FactSnapshot).where(FactSnapshot.organization_id == ctx.org_id,
        FactSnapshot.object_id == project_id, FactSnapshot.origin_key == origin_key,
        FactSnapshot.state.in_(['active', 'partial', 'unread', 'stale'])).values(state='superseded'))
    status = 'unread' if details and details.get('unread') else ('active' if (details or {}).get('complete', True) else 'partial')
    if old:
        old.state = status
        return old
    import uuid
    snap = FactSnapshot(organization_id=ctx.org_id, object_id=project_id, origin_key=origin_key,
        fingerprint=fingerprint, text_sha256=text_hash, file_sha256=file_hash,
        file_key=document.file_key if document else None, file_name=document.file_name if document else None,
        library_version_id=uuid.UUID(document.data['library_version_id']) if document and (document.data or {}).get('library_version_id') else None,
        document_id=document.id if document else None, intake_event_id=event.id if event else None,
        work_volume_id=volume.id if volume else None,
        title=title, kind=kind, state=status, method=method, details=details or {})
    db.add(snap)
    db.flush()
    from bis.services.executive_pipeline import _LOC, work_keys

    all_locations = locations(context_text) or (set().union(*(locations(c.quote) for c in claims)) if claims else set())
    families = {'кабель', 'бетон', 'армирование', 'изоляция', 'засыпка', 'сварка', 'трубопровод', 'кладка'}
    headline = next((line for line in context_text.splitlines() if line.strip()), '')
    primary_keys = work_keys(headline) & families
    primary_work = '|'.join(sorted(primary_keys)) if len(primary_keys) == 1 else ''
    single_place = _LOC.search(context_text) if len(all_locations) == 1 else None
    snap.details = {**snap.details, 'scope_location': single_place[0] if single_place else None, 'scope_work': primary_work}
    for n, claim in enumerate(claims):
        matches = [p for p in (pages or []) if claim.quote and _contains(p.content, claim.quote)]
        page_numbers = {p.page for p in matches}
        page = next(iter(page_numbers)) if len(page_numbers) == 1 else None
        locator = next((p for p in matches if p.page == page), None)
        location = claim.location
        # A single clearly stated place can scope a document's references, never a multi-area statement.
        loc_key = location_key(location) if location else location_key(claim.quote)
        if not loc_key and single_place and (category(kind, claim.field) != 'actual' or (kind == 'work_journal' and primary_work)):
            loc_key, location = next(iter(all_locations)), single_place[0]
        claim_work = work_key(claim.work or claim.quote)
        if not (set(claim_work.split('|')) & families) and primary_work:
            claim_work = primary_work
        db.add(ProjectFact(organization_id=ctx.org_id, object_id=project_id, snapshot_id=snap.id,
            ordinal=n, predicate=claim.field, category=category(kind, claim.field), value=claim.value,
            normalized_value=normalized(claim.field, claim.value), location=location,
            location_key=loc_key, work_key=claim_work,
            entity_key=normalized('material', claim.entity) if claim.entity else '', quote=claim.quote,
            page=page, chunk_id=uuid.UUID(locator.chunk_id) if locator else None))
    db.flush()
    return snap


def _contains(text, quote):
    from bis.services.executive_pipeline import norm
    return norm(quote) in norm(text)


def extractor_signature(ctx):
    from bis.services.executive_pipeline import FACT_SCHEMA_VERSION

    models = [(name, e.provider.model) for name, e in ctx.gateway.entries.items()] if ctx.gateway else []
    route = ctx.gateway.routes.get('executive_document', ctx.gateway.routes.get('default')) if ctx.gateway else None
    return digest([SCHEMA_VERSION, FACT_SCHEMA_VERSION, models, route])


def cached_snapshots(ctx, docs):
    ids = [d.id for d in docs]
    snapshots = ctx.db.scalars(select(FactSnapshot).where(FactSnapshot.organization_id == ctx.org_id,
        FactSnapshot.document_id.in_(ids), FactSnapshot.state.in_(['active', 'partial', 'unread', 'stale']))).all() if ids else []
    return {s.document_id: s for s in snapshots}


def is_current(ctx, doc, snapshot, text=None):
    return bool(snapshot and snapshot.state == 'active' and snapshot.file_key == doc.file_key
        and snapshot.kind == doc.kind and snapshot.details.get('extractor_signature') == extractor_signature(ctx)
        and (text is None or hashlib.sha256(text.encode()).hexdigest() == snapshot.text_sha256))


def index_document(ctx, doc, text=None, *, refresh=False, recover=True):
    from bis.services import executive_pipeline as p
    from bis.services.document_text import document_chunks, document_text

    if doc.organization_id != ctx.org_id or not doc.object_id or doc.kind not in DOCUMENT_KINDS or (doc.data or {}).get('executive_run_id'):
        raise ValueError('Для реестра нужен исходный документ выбранного проекта')
    project_id = scope(ctx, doc.object_id)
    # Serialize publication of a source; repeat imports must keep the same assertion IDs.
    locked = ctx.db.scalar(select(Document.id).where(Document.id == doc.id).with_for_update(skip_locked=True))
    if locked is None:
        raise SourceInProgress('Источник уже разбирается или изменился. Повторите проверку после завершения обработки')
    chunks = document_chunks(ctx.db, doc)
    text = text if text is not None else document_text(ctx.db, doc, ctx.storage)
    if not text.strip() and recover:
        text = p.read_source(ctx, doc)
        chunks = document_chunks(ctx.db, doc)
    file_hash, error = None, None
    if doc.file_key:
        try:
            file_hash = hashlib.sha256(ctx.storage.get(doc.file_key)).hexdigest()
        except (OSError, ValueError) as exc:
            error = str(exc)
    engine_signature = extractor_signature(ctx)
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    input_signature = digest([engine_signature, doc.kind, text_hash, file_hash, doc.file_key,
        [(c.page, c.content) for c in chunks]])
    marker = (doc.data or {}).get('facts_snapshot_id')
    import uuid
    cached = ctx.db.get(FactSnapshot, uuid.UUID(marker)) if marker else None
    if cached and cached.state == 'active' and cached.details.get('input_signature') == input_signature and not refresh:
        return cached
    if not text.strip() or error:
        snap = _publish(ctx, project_id=project_id, origin_key='document:' + str(doc.id),
            fingerprint=digest([input_signature, 'unread']), text_hash=text_hash, kind=doc.kind, title=doc.title,
            method='unread', claims=[], document=doc, file_hash=file_hash,
            details={'unread': True, 'complete': False, 'error': error or 'Нет извлечённого текста', 'input_signature': input_signature, 'extractor_signature': engine_signature})
    else:
        extracted, complete, method = p.source_facts(ctx, doc, text, refresh=refresh, persist=False)
        valid = [f for f in extracted.facts if p.verified_fact(f, text)]
        complete = complete and len(valid) == len(extracted.facts)
        snap = _publish(ctx, project_id=project_id, origin_key='document:' + str(doc.id),
            fingerprint=digest([input_signature, [f.model_dump() for f in valid], complete, method]),
            text_hash=text_hash, kind=doc.kind, title=doc.title, method=method, claims=valid, document=doc,
            file_hash=file_hash, pages=chunks, context_text=text,
            details={'complete': complete, 'input_signature': input_signature, 'extractor_signature': engine_signature, 'chunks': len(chunks),
                     'pages': sorted({c.page for c in chunks if c.page is not None}), 'claims': len(valid)})
    doc.data = {**(doc.data or {}), 'facts_snapshot_id': str(snap.id), 'facts_attempt_at': dt.datetime.now(dt.UTC).isoformat()}
    return snap


def snapshot_claims(ctx, snapshot):
    from bis.services.executive_pipeline import Fact, Facts, scope_matches
    rows = ctx.db.scalars(select(ProjectFact).where(ProjectFact.snapshot_id == snapshot.id,
        ProjectFact.organization_id == ctx.org_id, ProjectFact.object_id == snapshot.object_id).order_by(ProjectFact.ordinal)).all()
    return Facts(facts=[Fact(field=f.predicate, value=f.value, quote=f.quote, location=f.location if scope_matches(f.quote, f.location) else None) for f in rows])


def record_report(ctx, event, report, corrections=None):
    from bis.services.executive_pipeline import Fact
    project_id = scope(ctx, event.object_id)
    corrections = {k: v for k, v in (corrections or {}).items() if k in ("location", "materials", "batch", "work_start", "work_end")}
    claims = []
    for field in ('work_start', 'work_end', 'batch'):
        value = getattr(report, field)
        if value is not None and (field in corrections or field == 'batch' or str(value) in event.text or _relative_date_in_report(event.text)):
            claims.append(Fact(field=field, value=str(value), quote=event.text if field not in corrections else f'{field}: {value}',
                               location=report.location, work=report.work))
    for value in report.materials:
        claims.append(Fact(field='material', value=value, quote=event.text if not corrections.get('materials') else value,
                           location=report.location, work=report.work))
    # Only literal/parsed report facts belong here, never dates borrowed from journals later in the pipeline.
    claims.append(Fact(field='work', value=report.work, quote=event.text, location=report.location, work=report.work))
    return _publish(ctx, project_id=project_id, origin_key='report:' + str(event.id),
        fingerprint=digest([SCHEMA_VERSION, event.text, report.model_dump(mode='json'), corrections]),
        text_hash=hashlib.sha256(event.text.encode()).hexdigest(), kind='correction' if corrections else 'report',
        title='Сообщение о работе', method='user_correction' if corrections else 'report', claims=claims, event=event, context_text=event.text,
        details={'complete': True, 'revision': event.result.get('revision', 1), 'corrections': corrections,
                 'resolution_history': event.result.get('resolution_history', [])[-1:], 'report_text': event.text})


def _relative_date_in_report(text):
    from bis.services.executive_pipeline import dates_in
    return bool(dates_in(text)) or 'вчера' in text.lower() or 'сегодня' in text.lower()


def fact_out(fact, snapshot):
    return {'id': str(fact.id), 'object_id': str(fact.object_id), 'snapshot_id': str(snapshot.id), 'predicate': fact.predicate,
        'label': LABELS.get(fact.predicate, fact.predicate), 'category': fact.category,
        'value': fact.value, 'location': fact.location, 'location_key': fact.location_key, 'work_key': fact.work_key,
        'entity_key': fact.entity_key, 'quote': fact.quote, 'page': fact.page, 'source_title': snapshot.title,
        'document_id': str(snapshot.document_id) if snapshot.document_id else None,
        'report_id': str(snapshot.intake_event_id) if snapshot.intake_event_id else None,
        'work_volume_id': str(snapshot.work_volume_id) if snapshot.work_volume_id else None,
        'library_version_id': str(snapshot.library_version_id) if snapshot.library_version_id else None,
        'source_sha256': snapshot.file_sha256 or snapshot.text_sha256, 'method': snapshot.method,
        'state': snapshot.state}


def query(ctx, project_id=None, *, predicate=None, location=None, work=None, category_filter=None, limit=200):
    project_id = scope(ctx, project_id)
    stmt = current_query(ctx, project_id)
    if predicate:
        stmt = stmt.where(ProjectFact.predicate == predicate)
    if category_filter:
        stmt = stmt.where(ProjectFact.category == category_filter)
    if location:
        stmt = stmt.where(ProjectFact.location_key == location_key(location))
    if work:
        stmt = stmt.where(ProjectFact.work_key == work_key(work))
    rows = ctx.db.execute(stmt.order_by(FactSnapshot.created_at, ProjectFact.ordinal, ProjectFact.id).limit(min(limit, 1000) + 1)).all()
    return {'items': [fact_out(f, s) for f, s in rows[:limit]], 'truncated': len(rows) > limit}


def reconcile(ctx, project_id=None):
    project_id = scope(ctx, project_id)
    # One short project lock protects conflict/decision revisions; extraction itself locks sources separately.
    ctx.db.scalar(select(ConstructionObject.id).where(ConstructionObject.id == project_id).with_for_update())
    pairs = ctx.db.execute(current_query(ctx, project_id)).all()
    groups, materials = defaultdict(list), defaultdict(list)
    for f, _snap in pairs:
        if not f.location_key:
            continue
        if f.predicate in ('work_start', 'work_end') and f.category == 'actual' and f.work_key:
            groups[(f.predicate, f.location_key, f.work_key)].append(f)
        if f.predicate == 'act_kind' and f.category == 'requirement':
            groups[(f.predicate, f.location_key, f.work_key)].append(f)
        if f.predicate == 'material' and f.work_key:
            materials[(f.location_key, f.work_key)].append(f)
    candidates = []
    periods = defaultdict(dict)
    for (field, place, work), facts in groups.items():
        if field in ('work_start', 'work_end'):
            periods[(place, work)][field] = facts
            future = [f for f in facts if f.value > dt.date.today().isoformat()]
            if future:
                candidates.append(('invalid_date', field, place, work, future,
                    'Фактическая дата находится в будущем. Проверьте запись или её назначение.'))
    for (place, work), values in periods.items():
        starts, ends = values.get('work_start', []), values.get('work_end', [])
        if len({f.value for f in starts}) == 1 and len({f.value for f in ends}) == 1 and ends[0].value < starts[0].value:
            candidates.append(('invalid_period', 'period', place, work, starts + ends,
                'Окончание работ раньше начала. Требуется проверка исходных записей.'))
    for (field, place, work), facts in groups.items():
        if len({f.normalized_value for f in facts}) > 1:
            candidates.append(('different_values', field, place, work, facts,
                f'Различаются значения «{LABELS.get(field, field)}» для одного места и вида работ.'))
    for (place, work), facts in materials.items():
        planned = {f.normalized_value for f in facts if f.category == 'planned'}
        actual = {f.normalized_value for f in facts if f.category == 'actual'}
        if planned and actual and not actual <= planned:
            candidates.append(('material_mismatch', 'material', place, work, facts,
                'Фактические марки отличаются от предусмотренных проектом. Нужно основание для замены.'))
    existing = {c.group_key: c for c in ctx.db.scalars(select(FactConflict).where(
        FactConflict.organization_id == ctx.org_id, FactConflict.object_id == project_id))}
    seen = set()
    for kind, field, place, work, facts, message in candidates:
        group = digest([kind, field, place, work])
        ids = sorted(str(f.id) for f in facts)
        signature = digest(ids)
        seen.add(group)
        conflict = existing.get(group)
        if conflict is None:
            conflict = FactConflict(organization_id=ctx.org_id, object_id=project_id, group_key=group,
                kind=kind, predicate=field, location_key=place, work_key=work, state='open', revision=1,
                signature=signature, fact_ids=ids, message=message)
            ctx.db.add(conflict)
        elif conflict.signature != signature:
            conflict.signature, conflict.fact_ids, conflict.state = signature, ids, 'open'
            conflict.revision += 1
        elif conflict.state == 'stale':
            conflict.state = 'open'
            conflict.revision += 1
    for key, conflict in existing.items():
        if key not in seen and conflict.state != 'stale':
            conflict.state = 'stale'
            conflict.revision += 1
    ctx.db.flush()
    return list(ctx.db.scalars(select(FactConflict).where(FactConflict.organization_id == ctx.org_id,
        FactConflict.object_id == project_id, FactConflict.state != 'stale').order_by(FactConflict.created_at)))


def conflicts_out(ctx, conflicts):
    ids = [__import__('uuid').UUID(v) for c in conflicts for v in c.fact_ids]
    pairs = ctx.db.execute(select(ProjectFact, FactSnapshot).join(FactSnapshot, FactSnapshot.id == ProjectFact.snapshot_id)
        .where(ProjectFact.organization_id == ctx.org_id, ProjectFact.id.in_(ids))).all() if ids else []
    facts = {str(f.id): fact_out(f, s) for f, s in pairs}
    project_id = scope(ctx)
    active_ids = {str(row[0]) for row in ctx.db.execute(current_query(ctx, project_id).with_only_columns(ProjectFact.id))}
    result = []
    for c in conflicts:
        current_state = c.state if set(c.fact_ids) <= active_ids else 'stale'
        decision = ctx.db.scalar(select(FactDecision).where(FactDecision.conflict_id == c.id)
            .order_by(FactDecision.revision.desc()).limit(1))
        result.append({'id': str(c.id), 'kind': c.kind, 'predicate': c.predicate, 'message': c.message,
            'state': current_state, 'revision': c.revision, 'location_key': c.location_key, 'work_key': c.work_key,
            'facts': [facts[i] for i in c.fact_ids if i in facts],
            'selected_fact_id': str(decision.fact_id) if decision and current_state == 'resolved' and decision.signature == c.signature else None,
            'reason': decision.reason if decision and current_state == 'resolved' else None})
    return result


def decide(ctx, conflict_id, fact_id, revision, reason):
    from bis.services.audit import audit
    conflict = ctx.db.get(FactConflict, conflict_id)
    if conflict is None or conflict.organization_id != ctx.org_id:
        raise LookupError('Противоречие не найдено')
    project_id = scope(ctx, conflict.object_id)
    reconcile(ctx, project_id)
    ctx.db.refresh(conflict)
    if conflict.revision != revision or conflict.state == 'stale':
        raise ValueError('Противоречие изменилось. Обновите основания перед выбором')
    if len(reason.strip()) < 3:
        raise ValueError('Укажите содержательное основание выбора')
    if conflict.kind == 'material_mismatch':
        raise ValueError('Для замены материала требуется исправление данных или документ-основание, а не выбор значения')
    if conflict.kind != 'different_values':
        raise ValueError('Это противоречие требует исправления исходных сведений, а не выбора строки')
    if str(fact_id) not in conflict.fact_ids:
        raise ValueError('Выбранное значение не относится к этому противоречию')
    fact = ctx.db.scalar(current_query(ctx, project_id).with_only_columns(ProjectFact.id).where(ProjectFact.id == fact_id))
    if fact is None:
        raise ValueError('Основание устарело или недоступно')
    conflict.revision += 1
    conflict.state = 'resolved'
    decision = FactDecision(organization_id=ctx.org_id, object_id=project_id, conflict_id=conflict.id,
        fact_id=fact_id, revision=conflict.revision, signature=conflict.signature, reason=reason, user_id=ctx.user.id)
    ctx.db.add(decision)
    audit(ctx.db, ctx.org_id, actor_type='user', actor=str(ctx.user.id), action='brain.fact_decision',
        entity_type='fact_conflict', entity_id=conflict.id, details={'fact_id': str(fact_id), 'reason': reason})
    ctx.db.flush()
    return conflict


def preferred_values(ctx, location, work):
    project_id = scope(ctx)
    result = {}
    for conflict in reconcile(ctx, project_id):
        if conflict.state != 'resolved' or conflict.location_key != location_key(location) or conflict.work_key != work_key(work):
            continue
        decision = ctx.db.scalar(select(FactDecision).where(FactDecision.conflict_id == conflict.id,
            FactDecision.signature == conflict.signature).order_by(FactDecision.revision.desc()).limit(1))
        if decision and decision.fact_id:
            pair = ctx.db.execute(current_query(ctx, project_id).where(ProjectFact.id == decision.fact_id)).first()
            if pair and pair[0].predicate in ('work_start', 'work_end', 'act_kind'):
                result[pair[0].predicate] = fact_out(*pair) | {'decision_id': str(decision.id)}
    return result


def refresh_project(ctx, project_id=None, *, limit=20, force=False, checkpoint=None):
    project_id = scope(ctx, project_id)
    docs = ctx.db.scalars(source_documents(ctx, project_id).order_by(Document.created_at, Document.id)).all()
    cache = cached_snapshots(ctx, docs)
    pending = [d for d in docs if force or not is_current(ctx, d, cache.get(d.id))]
    pending.sort(key=lambda d: (0 if d.id not in cache or cache[d.id].state == 'stale' else 1,
                                (d.data or {}).get('facts_attempt_at') or '', d.created_at, str(d.id)))
    done, unread, partial, failed = [], [], [], []
    for doc in pending[:limit]:
        try:
            with ctx.db.begin_nested():
                snap = index_document(ctx, doc, refresh=force)
                done.append(str(doc.id))
                if snap.state == 'unread':
                    unread.append(str(doc.id))
                if snap.state == 'partial':
                    partial.append(str(doc.id))
        except Exception as exc:  # noqa: BLE001 — one unread source must not lose the investigation
            failed.append({'document_id': str(doc.id), 'error': type(exc).__name__})
        if checkpoint:
            checkpoint()
    index_work_volumes(ctx, project_id)
    conflicts = reconcile(ctx, project_id)
    return {'documents': len(docs), 'processed': len(done), 'cached': len(docs) - len(pending), 'remaining': max(0, len(pending) - limit),
        'unread': unread, 'partial': partial, 'failed': failed, 'conflicts': len([c for c in conflicts if c.state == 'open'])}


def index_work_volumes(ctx, project_id=None):
    from bis.services.executive_pipeline import Fact

    project_id = scope(ctx, project_id)
    rows = ctx.db.execute(select(WorkVolume, EstimateItem).join(EstimateItem, EstimateItem.id == WorkVolume.estimate_item_id)
        .where(WorkVolume.organization_id == ctx.org_id, WorkVolume.object_id == project_id,
               EstimateItem.organization_id == ctx.org_id, EstimateItem.object_id == project_id)).all()
    for row, item in rows:
        quantity = format(Decimal(str(row.quantity)).normalize(), 'f')
        details = {'quantity': quantity, 'date': row.date.isoformat(), 'work': item.name,
                   'unit': item.unit, 'location': row.location, 'status': row.status,
                   'complete': row.status != 'reported' and row.date <= dt.date.today(), 'estimate_item_id': str(item.id)}
        quote = f"{item.name}; {row.location or ''}; {row.date.isoformat()}; {row.quantity} {item.unit}; {row.status}"
        claims = [Fact(field=field, value=value, quote=quote, location=row.location, work=item.name)
                  for field, value in (('work', item.name), ('work_date', row.date.isoformat()),
                                       ('quantity', quantity), ('unit', item.unit))]
        _publish(ctx, project_id=project_id, origin_key='work_volume:' + str(row.id), fingerprint=digest(details),
            text_hash=digest(details), kind='work_volume', title='Запись журнала: ' + item.name,
            method='registry', claims=claims, volume=row, details=details, context_text=quote)
    return len(rows)


def preferred_dates(ctx, location, work):
    return {k: v for k, v in preferred_values(ctx, location, work).items() if k in ('work_start', 'work_end')}
