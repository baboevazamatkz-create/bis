"""Grounded requirements matrix. No embedded legal rules or Internet fallback."""
from __future__ import annotations

import re
import uuid
from types import SimpleNamespace

from sqlalchemy import or_, select

from bis.models import KnowledgeChunk, KnowledgeSource
from bis.models.libraries import LibraryVersion
from bis.services.library_access import active_versions

FIELDS = {'quality_requirement': 'Документ качества', 'protocol_requirement': 'Испытание',
          'scheme_requirement': 'Исполнительная схема', 'act_kind': 'Вид акта'}


def norm_sources(db, scope):
    return select(LibraryVersion, KnowledgeSource).join(KnowledgeSource, KnowledgeSource.id == LibraryVersion.source_id).where(
        LibraryVersion.id.in_(active_versions(scope)), LibraryVersion.form_kind.is_(None),
        or_(KnowledgeSource.kind == 'norm', KnowledgeSource.title.op('~*')(r'(^СП\s+\d|^ГОСТ|регламент|инструкци|стандарт|норматив)')),
        KnowledgeSource.status.notin_(['replaced', 'cancelled', 'rejected'])).order_by(LibraryVersion.id)


def catalog_signature(db, scope):
    from bis.services.project_facts import digest
    return digest([(str(v.id), v.sha256, s.status) for v, s in db.execute(norm_sources(db, scope))])


def modality(quote):
    if re.search(r'при условии|в случае|если\b|при наличии|при необходимости', quote, re.I):
        return 'conditional'
    if re.search(r'не требу[ею]т|не нуж|необязател|не обязател', quote, re.I):
        return 'not_required'
    return 'required'


def applicable(fact, text, report):
    from bis.services.executive_pipeline import locations, same_work, scope_matches
    from bis.services.project_facts import work_key
    from bis.services.project_structure import area_names
    if fact.location and not scope_matches(fact.location, report.location):
        return False
    if locations(fact.quote) and not scope_matches(fact.quote, report.location):
        return False
    if fact.work:
        return same_work(fact.work, report.work)
    if same_work(fact.quote, report.work):
        return True
    # Unlabelled clauses inherit a single work/place only; mixed specifications need explicit scope.
    if locations(text) and not scope_matches(text, report.location):
        return False
    families = {'кабель', 'бетон', 'армирование', 'изоляция', 'засыпка', 'сварка', 'трубопровод', 'кладка'}
    source_work = set(work_key(text.splitlines()[0] if text.splitlines() else '').split('|')) & families
    if len(source_work) != 1:
        source_work = set(work_key(text).split('|')) & families
    physical_places = locations('; '.join(area_names(text)))
    return len(source_work) == 1 and source_work <= set(work_key(report.work).split('|')) and len(physical_places) <= 1


def build(ctx, report, project_pairs, corpus):
    from bis.services import executive_pipeline as p
    from bis.services.project_facts import digest

    scope = ctx.db.info['source_context']
    items, errors = [], []
    inherited = []

    def add(fact, source, text):
        if fact.field not in FIELDS or not applicable(fact, text, report):
            return
        item = {'field': fact.field, 'label': FIELDS[fact.field], 'value': fact.value, 'quote': fact.quote,
                'modality': modality(fact.quote), 'source': source}
        item['id'] = digest(item)
        if not any(i['id'] == item['id'] for i in items):
            items.append(item)

    for doc, fact in project_pairs:
        add(fact, {'document_id': str(doc.id), 'title': doc.title, 'origin': 'project',
                   'library_version_id': (doc.data or {}).get('library_version_id')}, corpus[doc.id])
    sources = ctx.db.execute(norm_sources(ctx.db, scope)).all()
    for version, source in sources[:100]:
        chunks = ctx.db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.source_id == source.id)
                               .order_by(KnowledgeChunk.ord)).all()
        text = '\n'.join(c.content for c in chunks)
        if not text.strip() or len(text) > 500_000:
            errors.append({'field': 'rules_unread:' + str(source.id), 'message': 'Нормативный источник не разобран полностью: ' + source.title})
            continue
        if not p.same_work(source.title + '\n' + text, report.work):
            continue
        holder = SimpleNamespace(data=source.meta or {})
        parsed, complete, method = p.source_facts(ctx, holder, text, persist=False)
        source.meta = holder.data
        if not complete or source.status != 'active':
            errors.append({'field': 'rules_unconfirmed:' + str(source.id), 'message': 'Не подтверждены извлечение или редакция требований: ' + source.title})
        for fact in parsed.facts:
            before = len(items)
            add(fact, {'source_id': str(source.id), 'document_id': str(source.document_id) if source.document_id else None,
                'library_version_id': str(version.id), 'sha256': version.sha256, 'title': source.title, 'origin': 'library',
                'page': next((c.page for c in chunks if p.norm(fact.quote) in p.norm(c.content)), None), 'method': method}, text)
            if len(items) > before and items[-1]['modality'] == 'required':
                inherited.append(fact)
    if len(sources) > 100:
        errors.append({'field': 'rules_limit', 'message': 'Более 100 нормативных источников: проверка требований неполная'})
    for item in items:
        if item['modality'] == 'conditional':
            errors.append({'field': 'rule_condition:' + item['id'], 'message': 'Нужно подтвердить применимость условного требования: ' + item['value']})
    groups = {}
    for item in items:
        # Same subject/type with opposite modality is a conflict, never an implicit priority override.
        value = p.norm(item['value'])
        if item['field'] == 'quality_requirement':
            value = next((x for x in ('сертификат', 'паспорт', 'деклараци') if x in value), value)
        value = re.sub(r'\bне\s+требу[ею]т\w*|\bтребу[ею]т\w*|\bнеобязател\w*', '', value).strip(' .:;')
        group = (item['field'], value)
        groups.setdefault(group, []).append(item)
    for group in groups.values():
        if {'required', 'not_required'} <= {i['modality'] for i in group}:
            for item in group:
                item['conflict'] = True
            errors.append({'field': 'rule_conflict:' + group[0]['id'], 'message': 'Источники противоречат друг другу в обязательности: ' + group[0]['value']})
    return {'items': items, 'coverage': {'catalog_sources': len(sources), 'scanned_sources': min(len(sources), 100),
        'complete': not errors}, 'catalog_signature': catalog_signature(ctx.db, scope), 'errors': errors}, inherited


def bindings(report, matrix, evidence, attachments, corpus):
    """Record which selected document discharges each requirement; never treat a filename as proof."""
    from bis.services.executive_pipeline import _requirement_matches, material_matches, norm
    by_id = {a['id']: a for a in attachments}
    rows = []
    for item in matrix['items']:
        selected = []
        if item['modality'] == 'required' and not item.get('conflict'):
            for entry in evidence:
                doc_id = entry.get('document_id')
                if doc_id not in by_id:
                    continue
                text = corpus.get(uuid.UUID(doc_id), '')
                if item['field'] == 'quality_requirement' and entry['field'] == 'quality':
                    tags = [t for t in ('сертификат', 'паспорт', 'деклараци') if t in norm(item['value'])]
                    if not tags or any(t in norm(by_id[doc_id]['title'] + '\n' + text) for t in tags):
                        selected.append(doc_id)
                elif item['field'] == 'protocol_requirement' and entry['field'] == 'protocol' and _requirement_matches(item['value'], text) or item['field'] == 'scheme_requirement' and entry['field'] == 'scheme':
                    selected.append(doc_id)
        state = 'conflict' if item.get('conflict') else item['modality'] if item['modality'] != 'required' else 'satisfied' if selected else 'missing'
        if item['field'] == 'act_kind' and item['modality'] == 'required' and not item.get('conflict'):
            state = 'satisfied'  # validated against the resulting act kind by the pipeline
        if (item['field'] == 'quality_requirement' and state == 'satisfied'
                and any(not any(material_matches(m, corpus.get(uuid.UUID(d), '')) for d in selected) for m in report.materials)):
            state = 'missing'
        rows.append({**item, 'state': state, 'document_ids': list(dict.fromkeys(selected))})
    return rows
