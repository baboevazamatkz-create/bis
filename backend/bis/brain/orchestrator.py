"""Bounded, deterministic investigations before generation; never ask for data before looking."""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select

from bis.models import Document, KnowledgeChunk, KnowledgeSource
from bis.models.facts import BrainInvestigation, FactSnapshot, ProjectFact
from bis.services import project_facts as ledger

FIELD_KINDS = {
    'work_start': ('work_journal',), 'work_end': ('work_journal',),
    'work_date': ('work_journal',), 'work': ('work_journal',), 'material': ('work_journal', 'project_doc', 'certificate'),
    'batch': ('work_journal', 'certificate'), 'project_ref': ('project_doc',),
    'certificate_number': ('certificate',), 'certificate_expiry': ('certificate',),
    'protocol_result': ('test_protocol',), 'quantity': ('work_journal', 'project_doc'),
    'quality_requirement': ('project_doc',), 'protocol_requirement': ('project_doc',),
}
FAMILIES = {'кабель', 'бетон', 'армирование', 'изоляция', 'засыпка', 'сварка', 'трубопровод', 'кладка'}


def question_plan(question, focus=None):
    from bis.services.executive_pipeline import _LOC, norm, work_keys

    q = norm(question)
    focus = focus or {}
    # Accounting and closing have their own authoritative tools and calculations.
    accounting = bool(re.search(r'перерасход|списани|остатк|склад|поступлен|стоимост|\bкс[ -]?[23]\b|смет', q))
    write_request = bool(re.search(r'\b(?:сделай|составь|создай|оформи|подготовь|собери|заполни|измени|исправь|обнови|удали|зарегистрируй|отправь)\b', q))
    fields, category = [], None
    if re.search(r'когда|дат[аыуе]|срок|нача|заверш|закончи', q):
        if re.search(r'сертификат|паспорт', q) and re.search(r'действ|срок|истек', q):
            fields = ['certificate_expiry']
        elif re.search(r'нача|приступ', q):
            fields, category = ['work_start'], 'actual'
        elif re.search(r'заверш|закончи|оконча|пролож', q):
            fields, category = ['work_end'], 'actual'
    if not fields and re.search(r'какие работы|что.*(?:выполн|сделан)', q):
        fields, category = ['work'], 'actual'
    if not fields and re.search(r'объем|сколько.*(?:пролож|выполн|улож|залил)', q):
        fields, category = ['quantity'], 'planned' if re.search(r'по проект|по рд|проектн', q) else 'actual'
    if not fields and re.search(r'марка|материал|какой кабель|какая арматура|какой бетон', q):
        fields, category = ['material'], 'planned' if re.search(r'проект|\bрд\b|предусмотр', q) else 'actual'
    if not fields and re.search(r'партия|партии|партию', q):
        fields = ['batch']
        category = 'reference' if re.search(r'паспорт|сертификат', q) else 'actual'
    if not fields and re.search(r'шифр|лист.*(?:проект|рд)|какая рд|какой проект', q):
        fields = ['project_ref']
    if not fields and re.search(r'(?:номер|какой|найди).*сертификат|сертификат.*номер', q):
        fields = ['certificate_number']
    if not fields and re.search(r'результат.*испытан|заключение.*(?:испытан|протокол)|прошли.*испытан', q):
        fields = ['protocol_result']
    if not fields and re.search(r'по проект|по рд|рабоч.*документац', q) and re.search(r'какие.*(?:документ.*качеств|испытания.*нуж|протокол.*нуж)', q):
        fields = ['quality_requirement', 'protocol_requirement']
    if not fields and focus.get('fact_predicate') in FIELD_KINDS and len(q) < 160 and re.match(r'^(?:а\s+)?(?:на участке|участок|там\b|для этого|для этой)', q):
        fields, category = [focus['fact_predicate']], focus.get('fact_category') or None
    if accounting or write_request:
        fields, category = [], None
    loc = _LOC.search(question)
    work = work_keys(question) & FAMILIES
    inherited_location = focus.get('fact_location') if fields and not loc else None
    inherited_work = focus.get('fact_work', '') if fields and not work else ''
    return {'predicates': fields, 'category': category, 'location': loc[0] if loc else inherited_location,
            'work_key': '|'.join(sorted(work)) if work else inherited_work, 'source_kinds': list(dict.fromkeys(k for f in fields for k in FIELD_KINDS.get(f, ()))),
            'steps': ['Определить проект и предмет вопроса', 'Проверить реестр фактов',
                      'Дочитать подходящие исходные документы', 'Сопоставить значения и противоречия',
                      'Ответить с основаниями либо показать конкретный пробел']}


def investigate(ctx, question, object_id=None, *, budget=12, force=False):
    from bis.services.executive_pipeline import locations, scope_matches

    project_id = ledger.scope(ctx, object_id)
    plan = question_plan(question, ctx.focus)
    task = BrainInvestigation(organization_id=ctx.org_id, object_id=project_id,
        user_id=ctx.user.id if ctx.user else None, question=question, state='running', plan=plan, result={})
    ctx.db.add(task)
    ctx.db.flush()
    kinds = plan['source_kinds']
    docs = ctx.db.scalars(ledger.source_documents(ctx, project_id).where(
        Document.kind.in_(kinds) if kinds else False).order_by(Document.created_at, Document.id)).all()
    texts = defaultdict(list)
    if docs:
        rows = ctx.db.execute(select(KnowledgeSource.document_id, KnowledgeChunk.content).join(
            KnowledgeChunk, KnowledgeChunk.source_id == KnowledgeSource.id).where(
            KnowledgeSource.organization_id == ctx.org_id, KnowledgeSource.document_id.in_([d.id for d in docs]))
            .order_by(KnowledgeSource.created_at, KnowledgeSource.id, KnowledgeChunk.ord)).all()
        for doc_id, content in rows:
            texts[doc_id].append(content)
    excluded, candidates = [], []
    for doc in docs:
        text = '\n'.join(texts[doc.id])
        places = locations(doc.title + '\n' + text)
        if plan['location'] and places and not scope_matches(doc.title + '\n' + text, plan['location']):
            excluded.append(str(doc.id))
        else:
            candidates.append((doc, text))
    candidates.sort(key=lambda pair: (
        0 if plan['location'] and scope_matches(pair[0].title, plan['location']) else 1,
        kinds.index(pair[0].kind) if pair[0].kind in kinds else 99, pair[0].created_at, str(pair[0].id)))
    cache = ledger.cached_snapshots(ctx, [doc for doc, _ in candidates])
    already_read = [(doc, text) for doc, text in candidates if not force and ledger.is_current(ctx, doc, cache.get(doc.id), text)]
    cached_ids = {doc.id for doc, _ in already_read}
    pending = [(doc, text) for doc, text in candidates if doc.id not in cached_ids]
    read, unread, partial, failed = [str(d.id) for d, _ in already_read], [], [], []
    for doc, text in pending[:budget]:
        if ctx.progress:
            ctx.progress({'type': 'tool', 'tool': 'investigate_project', 'label': 'Сверяю источник: ' + doc.title})
        try:
            with ctx.db.begin_nested():
                snapshot = ledger.index_document(ctx, doc, text or None, refresh=force)
                read.append(str(doc.id))
                if snapshot.state == 'unread':
                    unread.append({'id': str(doc.id), 'title': doc.title, 'reason': snapshot.details.get('error')})
                elif snapshot.state == 'partial':
                    partial.append({'id': str(doc.id), 'title': doc.title})
        except Exception as exc:  # noqa: BLE001 — preserve an explainable partial investigation
            failed.append({'id': str(doc.id), 'title': doc.title, 'reason': str(exc) if isinstance(exc, ledger.SourceInProgress) else type(exc).__name__})
    ledger.index_work_volumes(ctx, project_id)
    conflicts = ledger.conflicts_out(ctx, ledger.reconcile(ctx, project_id))
    relevant = [c for c in conflicts if (not plan['location'] or c['location_key'] == ledger.location_key(plan['location']))
                and (not plan['work_key'] or c['work_key'] == plan['work_key'])
                and (not plan['predicates'] or c['predicate'] in plan['predicates'] or c['predicate'] == 'period')]
    stmt = ledger.current_query(ctx, project_id)
    if plan['predicates']:
        stmt = stmt.where(ProjectFact.predicate.in_(plan['predicates']))
    if plan['category']:
        stmt = stmt.where(ProjectFact.category == plan['category'])
    if plan['location']:
        stmt = stmt.where(ProjectFact.location_key == ledger.location_key(plan['location']))
    if plan['work_key']:
        stmt = stmt.where(ProjectFact.work_key == plan['work_key'])
    pairs = ctx.db.execute(stmt.order_by(FactSnapshot.created_at, ProjectFact.ordinal, ProjectFact.id).limit(301)).all()
    facts = [ledger.fact_out(f, s) for f, s in pairs[:300]]
    for conflict in relevant:
        if conflict['state'] == 'resolved':
            rejected = {f['id'] for f in conflict['facts']} - {conflict['selected_fact_id']}
            facts = [f for f in facts if f['id'] not in rejected]
    missing = [field for field in plan['predicates'] if not any(f['predicate'] == field for f in facts)]
    remaining = max(0, len(pending) - budget)
    state = ('unsupported' if not plan['predicates'] else
             'conflict' if any(c['state'] == 'open' for c in relevant) else
             'partial' if unread or partial or failed or remaining or len(pairs) > 300 or any(f['state'] != 'active' for f in facts) else
             'needs_data' if missing or not facts else 'ready')
    # Singular dates without a precise work/place remain concrete alternatives, never an arbitrary earliest/latest date.
    if (state == 'ready' and any(f in ('work_start', 'work_end') for f in plan['predicates'])
            and len({(f['location_key'], f['work_key'], f['value']) for f in facts}) > 1):
        state = 'ambiguous'
    result = {'id': str(task.id), 'object_id': str(project_id), 'state': state, 'plan': plan, 'facts': facts,
        'conflicts': relevant, 'missing': missing, 'coverage': {'candidates': len(candidates), 'read': len(read),
            'remaining': remaining, 'cached': len(already_read), 'excluded_other_locations': len(excluded), 'unread': unread, 'partial': partial,
            'failed': failed, 'truncated_facts': len(pairs) > 300}, 'source_document_ids': read}
    task.state, task.result = state, result
    if plan['location']:
        ctx.focus['fact_location'] = plan['location']
    if plan['work_key']:
        ctx.focus['fact_work'] = plan['work_key']
    if len(plan['predicates']) == 1:
        ctx.focus['fact_predicate'] = plan['predicates'][0]
        ctx.focus['fact_category'] = plan['category'] or ''
    ctx.trace.append({'tool': 'investigate_project', 'arguments': {'question': question, 'object_id': str(project_id)},
        'error': False, 'phase': 'orchestration', 'investigation_id': str(task.id), 'coverage': result['coverage'], 'state': state})
    return result


def cite(ctx, fact):
    number = ctx.add_citation({'chunk_id': 'fact:' + fact['id'], 'fact_id': fact['id'],
        'fact_snapshot_id': fact['snapshot_id'] if fact.get('document_id') else None, 'object_id': fact.get('object_id'),
        'document_id': fact['document_id'], 'source_id': None, 'content': fact['quote'], 'page': fact['page'],
        'title': fact['source_title'], 'citation': fact['source_title'] + (f", стр. {fact['page']}" if fact['page'] else ''),
        'status': 'active' if fact['state'] == 'active' else 'unknown', 'section_path': None})
    return f'[{number}]'


def answer(ctx, result):
    plan, facts = result['plan'], result['facts']
    if result['state'] == 'unsupported':
        return ('В реестре можно сверить даты, выполненные работы, материалы, объёмы, РД, документы качества и результаты испытаний. '
                'Для другого вопроса используйте диалог с Синапсом — он располагает инструментами поиска по архиву.')
    where = (' для ' + plan['location']) if plan['location'] else ''
    if result['state'] == 'conflict':
        lines = ['В источниках есть противоречие' + where + '.']
        for conflict in result['conflicts']:
            if conflict['state'] == 'open':
                lines.append(conflict['message'])
                lines.extend('• ' + _display(f['value'], f['predicate']) + ' — ' + f['source_title'] + ' ' + cite(ctx, f) for f in conflict['facts'])
        lines.append('Сверьте основания в разделе «Факты проекта». Исходные данные сохранены без подмены.')
        return '\n'.join(lines)
    if result['state'] == 'ambiguous':
        return ('Найдено несколько вариантов' + where + ':\n' + '\n'.join(
            '• ' + _display(f['value'], f['predicate']) + ' — ' + (f['location'] or f['location_key'] or 'место не указано') +
            ' / ' + (f['work_key'] or 'работа не уточнена') + ' ' + cite(ctx, f) for f in facts)
            + '\nУточните, какая из перечисленных работ нужна.')
    if result['state'] == 'partial':
        coverage = result['coverage']
        lines = ['Проверка источников ещё неполная.']
        if coverage['remaining']:
            lines.append(f"Не проверено подходящих документов: {coverage['remaining']}.")
        lines.extend('Не прочитан файл: ' + d['title'] for d in coverage['unread'])
        lines.extend('Требуется повторная проверка извлечения: ' + d['title'] for d in coverage['partial'])
        lines.extend('Не завершена проверка: ' + d['title'] + ' — ' + d['reason'] for d in coverage['failed'])
        if facts:
            lines.append('Уже найдены следующие сведения:')
            lines.extend('• ' + f['label'] + ': ' + _display(f['value'], f['predicate']) + ' ' + cite(ctx, f) for f in facts[:12])
        return '\n'.join(lines)
    if result['state'] == 'needs_data':
        missing = ', '.join(ledger.LABELS.get(f, f).lower() for f in result['missing']) or 'нужные сведения'
        return ('Проверены доступные источники' + where + f', но не подтверждены: {missing}. '
            'Отсутствие записи не заменяется проектным предположением. Результат поиска сохранён в «Фактах проекта».')
    if plan['predicates'] == ['quantity']:
        return _quantity_answer(ctx, facts, where)
    unique = defaultdict(list)
    for f in facts:
        unique[(f['predicate'], ledger.normalized(f['predicate'], f['value']))].append(f)
    lines = []
    for group in unique.values():
        f = group[0]
        refs = ' '.join(cite(ctx, value) for value in group[:3])
        lines.append(f['label'] + where + ': ' + _display(f['value'], f['predicate']) + '. ' + refs)
    return '\n'.join(lines)


def _display(value, field):
    if field in ('work_start', 'work_end', 'work_date', 'certificate_expiry'):
        try:
            return dt.date.fromisoformat(value).strftime('%d.%m.%Y')
        except ValueError:
            pass
    return value


def _quantity_answer(ctx, facts, where):
    # Aggregate only the authoritative row registry; document/report quantities may describe the same work again.
    rows = [f for f in facts if f.get('work_volume_id') and f['state'] == 'active']
    if not rows:
        ids = [__import__('uuid').UUID(f['snapshot_id']) for f in facts]
        units = defaultdict(set)
        for fact in ctx.db.scalars(select(ProjectFact).where(ProjectFact.snapshot_id.in_(ids), ProjectFact.predicate == 'unit')):
            units[str(fact.snapshot_id)].add(fact.value)
        return '\n'.join(('Проектный объём' if f['category'] == 'planned' else 'Объём') + where + ': ' + f['value'] +
            ' ' + (next(iter(units[f['snapshot_id']])) if len(units[f['snapshot_id']]) == 1 else '(единица не определена однозначно)') +
            '. ' + cite(ctx, f) for f in facts)
    totals = defaultdict(Decimal)
    refs = defaultdict(list)
    for fact in rows:
        snapshot = ctx.db.get(FactSnapshot, __import__('uuid').UUID(fact['snapshot_id']))
        unit = snapshot.details['unit']
        totals[unit] += Decimal(fact['value'].replace(',', '.'))
        refs[unit].append(cite(ctx, fact))
    return '\n'.join(f'Учтённый объём{where}: {value.normalize():f} {unit}. ' + ' '.join(refs[unit]) for unit, value in totals.items())
