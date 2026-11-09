"""Match a work/act to scoped design items; never merge different circuits or sum unrelated rows."""
from __future__ import annotations

import datetime as dt
import re
import uuid
from collections import defaultdict

from sqlalchemy import select

from bis.models import Document
from bis.models.project_reading import ProjectReading
from bis.services import project_reading as reader
from bis.services import project_structure as structure
from bis.services.executive_pipeline import material_key, norm, scope_matches
from bis.services.library_access import SourceContext, document_predicate


def one(values):
    values = list(dict.fromkeys(v for v in values if v))
    return values[0] if len(values) == 1 else None


def target(work, location=None, materials=None, quantity=None, unit=None, element=None, work_end=None):
    areas = structure.area_names(work or '')
    match = re.search(r'объ[её]м\s*:\s*(\d+(?:[.,]\d+)?)\s*([^\s;,]+)', work or '', re.I)
    return {'work': work, 'location': location or one(areas), 'element': element or one(structure.element_codes(work or '')),
            'materials': list({material_key(m): m for m in materials or []}.values()), 'quantity': str(quantity) if quantity is not None else match[1] if match else None,
            'unit': unit or (match[2] if match else None), 'work_end': str(work_end) if work_end else None}


def page_claims(page, field):
    return [c['value'] for c in page['claims'] if c['field'] == field]


def book_value(reading, field):
    return one([value for page in reading.payload['pages'] for value in page_claims(page, field)])


def complementary(ctx, documents):
    seen = set()
    for doc in documents:
        reading = reader.current(ctx, doc.id)
        if reading is None or reading.state != 'ready':
            return False
        keys = {(one(page_claims(p, 'code')) or book_value(reading, 'code'), one(page_claims(p, 'sheet')))
                for p in reading.payload['pages']}
        if any(not code or not sheet for code, sheet in keys) or seen & keys:
            return False
        seen |= keys
    return bool(seen)


def page_place(page):
    from bis.services.executive_pipeline import locations

    values = page_claims(page, 'location')
    families = defaultdict(set)
    for value in values:
        for key in locations(value):
            family, identifier = key.split(':', 1)
            families[family].add(identifier)
    return '; '.join(dict.fromkeys(values)) if families and all(len(v) == 1 for v in families.values()) else None


def effective_pages(readings, on_date=None):
    entries, grouped = [], defaultdict(list)
    for reading in readings:
        if on_date and reading.payload.get('document_date') and reading.payload['document_date'] > str(on_date):
            continue
        for page in reading.payload['pages']:
            entries.append((reading, page))
            code, sheet = one(page_claims(page, 'code')) or book_value(reading, 'code'), one(page_claims(page, 'sheet'))
            if code and sheet:
                grouped[(norm(code), norm(sheet))].append((reading, page))
    excluded = set()
    for group in grouped.values():
        revisions = [one(page_claims(p, 'revision')) for _, p in group]
        if len(group) > 1 and all(str(r or '').isdigit() for r in revisions):
            maximum = max(int(r) for r in revisions)
            excluded.update((str(r.id), p['ordinal']) for (r, p), rev in zip(group, revisions, strict=True) if int(rev) < maximum)
    return [(r, p) for r, p in entries if (str(r.id), p['ordinal']) not in excluded]


def design_items(readings, on_date=None):
    rows, element_places = [], defaultdict(set)
    proofs = defaultdict(list)
    for reading, page in effective_pages(readings, on_date):
        code = one(page_claims(page, 'code')) or book_value(reading, 'code')
        stage = one(page_claims(page, 'stage')) or book_value(reading, 'stage')
        family = (norm(code) if code else str(reading.document_id)) + '|' + (stage or 'unknown')
        place = page_place(page)
        element = one(page_claims(page, 'element'))
        for declared_element in page_claims(page, 'element') if place else []:
            element_places[(family, norm(declared_element))].add(place)
            proofs[(family, norm(declared_element))].append({'reading_id': str(reading.id), 'page': page['physical_page'],
                'quote': '\n'.join(c['quote'] for c in page['claims'] if c['field'] in ('location', 'element'))})
        for item in page['items']:
            location = item['location']
            if location and item.get('location_label') and not structure.area_names(location):
                location = item['location_label'] + ' ' + location
            row = {**item, 'location': location or place, 'element': item['element'] or element,
                'stage': stage,
                'code': code, 'sheet': one(page_claims(page, 'sheet')), 'revision': one(page_claims(page, 'revision')),
                'reading_id': str(reading.id), 'document_id': str(reading.document_id), 'page': page['physical_page'],
                'page_label': page['label'], 'family': family, 'scope_method': 'explicit' if item['location'] or item['element'] else 'single_page_context'}
            rows.append(row)
    for row in rows:
        key = (row['family'], norm(row['element']))
        if not row['location'] and row['element'] and len(element_places[key]) == 1:
            row['location'] = next(iter(element_places[key]))
            row['scope_method'], row['scope_evidence'] = 'explicit_element_link', proofs[key]
    # Rule/model duplicates describe the same source row once; distinct positions/elements stay distinct.
    return list({(r['reading_id'], r['page_label'], norm(r['quote']), r['position'], r['kind']): r for r in rows}.values())


def item_material(row):
    name, mark = row['name'], row.get('mark')
    return name if not mark or norm(mark) in norm(name) else name + ' ' + mark


def material_matches_row(row, material):
    from bis.services.executive_pipeline import material_matches

    mark = row.get('mark')
    if not mark:
        return (material_key(row['name']) == material_key(material)
                or any(f.field == 'material' and material_key(f.value) == material_key(material)
                       for f in structure.heuristic_facts(row['name']).facts))
    if any(c.isdigit() for c in mark):
        matched = material_matches(mark, material)
    else:
        matched = bool(re.search(r'(?<!\w)' + re.escape(norm(mark)) + r'(?!\w)', norm(material)))
    if not matched:
        return False
    # Diameters/dimensions can be in the name column while the grade is in the mark column.
    sizes = re.findall(r'(?:[døф]\s*\d+(?:[.,]\d+)?|\d+\s*[хx×]\s*\d+(?:[.,]\d+)?)', norm(row['name']))
    return all(material_key(size) in material_key(material) for size in sizes)


def unit_key(value):
    return re.sub(r'[\s.]', '', norm(value)).replace('м³', 'м3').replace('м²', 'м2').replace('погм', 'м').replace('пм', 'м')


def compare(target_work, readings):
    all_rows = design_items(readings, target_work.get('work_end') or dt.date.today())
    scoped, unscoped = [], []
    for row in all_rows:
        if row.get('stage') in ('П', 'ПД'):
            unscoped.append(row)
            continue
        if target_work.get('location') and row['location'] and not scope_matches(row['location'], target_work['location']):
            continue
        if target_work.get('element') and row['element'] and norm(row['element']) != norm(target_work['element']):
            continue
        if (target_work.get('location') and not row['location']) or (target_work.get('element') and not row['element']):
            unscoped.append(row)
        else:
            scoped.append(row)
    checks, issues = [], []
    stated = one(structure.area_names(target_work.get('work') or ''))
    if not target_work.get('location') and not target_work.get('element'):
        issues.append({'field': 'project_scope_unknown', 'message': 'В работах не определены место или идентификатор элемента. Одной марки материала недостаточно для привязки к проекту'})
    if stated and target_work.get('location') and not scope_matches(stated, target_work['location']):
        issues.append({'field': 'project_act_scope', 'message': 'Место в описании работ расходится с выбранной областью акта'})
    if target_work.get('element') and any(norm(e) != norm(target_work['element']) for e in structure.element_codes(target_work.get('work') or '')):
        issues.append({'field': 'project_act_element', 'message': 'Описание работ содержит другой элемент или несколько линий. Уточните состав работ акта'})
    multiple_quantity = target_work.get('quantity') is not None and len(target_work.get('materials', [])) > 1
    if multiple_quantity:
        issues.append({'field': 'project_quantity_scope', 'message': 'Общий объём нельзя приписать нескольким материалам. Нужны отдельные объёмы по позициям'})
    for material in target_work.get('materials', []):
        matches = [r for r in scoped if material_matches_row(r, material)]
        # A specification row is more precise than the same mark repeated in its own notes.
        specified = [r for r in matches if r['kind'] == 'specification']
        matches = specified or matches
        state = 'matched' if len(matches) == 1 else 'ambiguous' if matches else 'unconfirmed'
        if not matches and scoped:
            state = 'mismatch'
            issues.append({'field': 'project_material', 'message': 'Фактическая марка не совпадает с позициями РД в выбранной области: ' + material})
        elif not matches:
            issues.append({'field': 'project_scope', 'message': 'Нет подтверждённой привязки материала к месту/элементу проекта: ' + material})
        elif len(matches) > 1:
            issues.append({'field': 'project_position', 'message': 'Марка встречается в нескольких позициях/элементах. Выберите конкретную линию или позицию: ' + material})
        if len(matches) == 1 and structure.decimal(matches[0]['quantity']) == 0:
            issues.append({'field': 'project_zero_quantity', 'message': 'Позиция с нулевым проектным количеством не подтверждает применение материала'})
            state = 'mismatch'
        if len(matches) == 1 and target_work.get('quantity') is not None and not multiple_quantity:
            row = matches[0]
            actual, planned = structure.decimal(target_work['quantity']), structure.decimal(row['quantity'])
            if actual is None or planned is None or not target_work.get('unit') or not row['unit']:
                issues.append({'field': 'project_quantity_unknown', 'message': 'Количество или единица не подтверждены однозначной строкой спецификации'})
                state = 'unconfirmed'
            elif unit_key(target_work['unit']) != unit_key(row['unit']):
                issues.append({'field': 'project_unit', 'message': 'Единица объёма в сообщении не совпадает со спецификацией'})
                state = 'mismatch'
            elif actual > planned:
                issues.append({'field': 'project_quantity', 'message': f'Объём {actual} превышает объём позиции РД {planned} {row["unit"]}'})
                state = 'mismatch'
        checks.append({'material': material, 'state': state, 'identity_matched': bool(matches), 'candidates': matches or scoped[:10]})
    if not target_work.get('materials'):
        issues.append({'field': 'project_actual_material', 'message': 'Фактические материалы не подтверждены; проектные марки не подставлены вместо них'})
    partial = not readings or any(r.state != 'ready' for r in readings)
    if partial:
        issues.append({'field': 'project_reading_partial', 'message': 'Не все страницы проекта разобраны и подтверждены. Продолжите чтение проекта'})
    state = 'conflict' if any(c['state'] == 'mismatch' for c in checks) else 'ambiguous' if any(c['state'] == 'ambiguous' for c in checks) else 'partial' if partial else 'insufficient' if issues else 'aligned'
    return {'state': state, 'target': target_work, 'checks': checks, 'issues': issues,
            'unscoped_items': len(unscoped), 'planned_items': scoped,
            'readings': [{'id': str(r.id), 'document_id': str(r.document_id), 'result_sha256': r.result_sha256} for r in readings],
            'limits': 'Проверяются подтверждённые надписи и позиции. Геометрия, накопленные объёмы прежних актов и разрешение на последующие работы не выводятся из этой сверки.'}


def for_plan(ctx, report, project_documents, actual_materials, *, budget=8):
    readings, errors = [], []
    remaining = budget
    for doc in project_documents:
        try:
            cached = reader.current(ctx, doc.id)
            if cached and cached.state == 'ready':
                readings.append(cached)
            elif remaining:
                before = cached.payload['coverage']['processed'] if cached else 0
                reading = reader.read(ctx, doc.id, budget=remaining)
                readings.append(reading)
                remaining -= max(1, reading.payload['coverage']['processed'] - before)
            elif cached:
                readings.append(cached)
                errors.append({'field': 'project_reading_budget', 'message': 'Лимит чтения проекта исчерпан; чтение продолжится отдельной порцией'})
            else:
                errors.append({'field': 'project_reading_budget', 'message': 'Остались непрочитанные проектные документы: ' + doc.title})
        except (ValueError, OSError) as exc:
            errors.append({'field': 'project_reading_unavailable', 'message': 'Не удалось разобрать проект: ' + doc.title + '. ' + str(exc)})
    result = compare(target(report.work, report.location, actual_materials, element=getattr(report, 'element', None), work_end=report.work_end), readings)
    result['issues'] += errors
    result['references'] = []
    for reading, page in effective_pages(readings, report.work_end):
        code = one(page_claims(page, 'code')) or book_value(reading, 'code')
        sheet = one(page_claims(page, 'sheet'))
        if code:
            code_page = page if code in page_claims(page, 'code') else next(p for p in reading.payload['pages'] if code in page_claims(p, 'code'))
            code_quotes = [c['quote'] for c in code_page['claims'] if c['field'] == 'code' and c['value'] == code]
            sheet_quotes = [c['quote'] for c in page['claims'] if c['field'] == 'sheet']
            result['references'].append({'code': code, 'sheet': sheet, 'document_id': str(reading.document_id),
                'page': page['physical_page'] if code_page['physical_page'] == page['physical_page'] else None,
                'reading_id': str(reading.id), 'quote': '\n'.join(dict.fromkeys([*code_quotes, *sheet_quotes])),
                'proof_pages': [{'page': code_page['physical_page'], 'quotes': code_quotes}, {'page': page['physical_page'], 'quotes': sheet_quotes}]})
    if errors and result['state'] == 'aligned':
        result['state'] = 'partial'
    return result


def compare_act(ctx, act):
    from bis.services.project_facts import scope
    scope(ctx, act.object_id)
    docs = ctx.db.scalars(select(Document).where(document_predicate(SourceContext(ctx.org_id, act.object_id, True)),
        Document.object_id == act.object_id, Document.kind == 'project_doc', Document.status.notin_(['cancelled', 'replaced', 'rejected']))).all()
    # Generated acts persist the exact selected RD set. Do not silently replace it with unrelated files.
    selected = {x['document_id'] for x in (act.data or {}).get('project_alignment', {}).get('readings', [])}
    if selected:
        docs = [d for d in docs if str(d.id) in selected]
    readings = [reader.current(ctx, d.id) for d in docs]
    data = act.data or {}
    previous_target = data.get('project_alignment', {}).get('target') or {}
    work = data.get('works', '')
    fallback_place = previous_target.get('location') if work == previous_target.get('work') else None
    work_target = target(work, location=one(structure.area_names(work)) or fallback_place,
        materials=[m.split(' — ')[0] for m in data.get('materials', [])], work_end=data.get('work_end'))
    result = compare(work_target, [r for r in readings if r])
    if any(r is None for r in readings) or (selected and len(docs) != len(selected)):
        result['state'] = 'partial'
        result['issues'].append({'field': 'project_reading_stale', 'message': 'Проект изменился или ещё не разобран. Обновите чтение и пересоберите акт'})
    return result


def validate_references(db, doc):
    from bis.services.library_access import bind_context

    if not (doc.data or {}).get('project_alignment'):
        return
    selected = db.info.get('source_context')
    if selected and selected.object_id and selected.object_id != doc.object_id:
        raise ValueError('Акт относится к другому активному проекту')
    bind_context(db, doc.organization_id, doc.object_id)
    recorded = (doc.data or {}).get('project_alignment', {}).get('act_fields_sha256')
    if recorded and recorded != act_fields_signature(doc):
        raise ValueError('Сведения подготовленного акта изменились после сверки с РД. Повторите проверку и сборку')
    for ref in (doc.data or {}).get('project_alignment', {}).get('readings', []):
        row = db.get(ProjectReading, uuid.UUID(ref['id']))
        source = db.get(Document, uuid.UUID(ref['document_id']))
        if (row is None or source is None or row.organization_id != doc.organization_id or row.object_id != doc.object_id
                or row.result_sha256 != ref['result_sha256'] or (source.data or {}).get('project_reading_id') != ref['id']):
            raise ValueError('Структура прочитанного проекта изменилась. Повторите сверку и сборку акта')


def act_fields_signature(doc):
    from bis.services.project_facts import digest

    return digest({key: (doc.data or {}).get(key) for key in ('works', 'materials', 'project_docs', 'work_start', 'work_end')})
