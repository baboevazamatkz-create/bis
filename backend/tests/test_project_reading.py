"""Sheet provenance, specification columns, scope links and generated-act comparisons."""
import io
import json

import pytest
from sqlalchemy import func, select
from test_executive_pipeline import context as context  # noqa: F401
from test_executive_pipeline import corpus as corpus  # noqa: F401
from test_executive_pipeline import run, source
from test_project_facts import replace_text

from bis.models import ConstructionObject, Document, User
from bis.models.project_reading import ProjectReading
from bis.services import project_alignment as alignment
from bis.services import project_reading as reader
from bis.services import project_structure as structure
from bis.services.documents import save_document

TABLE = '''Шифр РД: 17-2026-ЭМ
Лист 7; Изм. 1
Участок 123. Линия КЛ-1
Поз. | Наименование | Марка | Ед. | Кол-во | Линия
1 | Кабель силовой | ВВГнг-LS 3х2,5 | м | 100 | КЛ-1
2 | Кабель силовой | ВВГнг-LS 3х25 | м | 250 | КЛ-2'''


def test_table_columns_preserve_dimensions_and_quantities():
    result = structure.read_page(None, TABLE)
    rows = [i for i in result['items'] if i['kind'] == 'specification']
    assert [(r['position'], r['quantity'], r['element']) for r in rows] == [('1', '100', 'КЛ-1'), ('2', '250', 'КЛ-2')]
    assert rows[0]['mark'] == 'ВВГнг-LS 3х2,5'
    assert result['complete']


def test_xlsx_keeps_blank_column_positions_and_sheet_names(context):
    import openpyxl
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = 'Спецификация ЭМ'
    sheet.append(['Поз.', 'Наименование', 'Марка', 'Примечание', 'Ед.', 'Кол-во', 'Участок'])
    sheet.append(['1', 'Кабель', 'ВВГнг-LS 3х2,5', None, 'м', 120, 123])
    raw = io.BytesIO()
    book.save(raw)
    obj = context.db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == context.org_id))
    doc = save_document(context.db, context.storage, org_id=context.org_id, object_id=obj.id, kind='project_doc', title='Спецификация',
        data={}, file_bytes=raw.getvalue(), file_name='spec.xlsx', created_by='test:project')
    reading = reader.read(context, doc.id)
    item = alignment.design_items([reading])[0]
    assert item['quantity'] == '120' and item['unit'] == 'м' and item['location'] == 'Участок 123'
    assert item['page'] is None and item['page_label'] == 'Спецификация ЭМ'


def test_model_cannot_exchange_qty_and_grade_digits(context):
    from bis.llm.gateway import LLMGateway, ProviderEntry
    from bis.llm.providers.offline import ScriptedProvider
    provider = ScriptedProvider([json.dumps({'items': [{'position': '1', 'name': 'Кабель силовой', 'mark': 'ВВГнг-LS 3х2,5',
        'unit': 'м', 'quantity': '2.5', 'quote': TABLE.splitlines()[4]}]})])
    context.gateway = LLMGateway(entries={'test': ProviderEntry(provider, 'confidential')})
    parsed = structure.read_page(context.gateway, TABLE)
    assert parsed['rejected'] and not parsed['complete']
    assert {i['quantity'] for i in parsed['items']} == {'100', '250'}


def test_same_material_different_circuits_stays_ambiguous(context):
    doc = source(context, 'project_doc', 'РД', TABLE.replace('3х25', '3х2,5'))
    reading = reader.read(context, doc.id)
    no_circuit = alignment.compare(alignment.target('Прокладка кабеля', 'участок 123', ['кабель ВВГнг-LS 3х2,5']), [reading])
    assert no_circuit['state'] == 'ambiguous'
    exact = alignment.compare(alignment.target('Прокладка кабеля', 'участок 123', ['кабель ВВГнг-LS 3х2,5'], '110', 'м', 'КЛ-1'), [reading])
    assert exact['state'] == 'conflict' and any(i['field'] == 'project_quantity' for i in exact['issues'])
    assert len(exact['checks'][0]['candidates']) == 1
    allowed = alignment.compare(alignment.target('Прокладка кабеля', 'участок 123', ['кабель ВВГнг-LS 3х2,5'], '90', 'м', 'КЛ-1'), [reading])
    assert allowed['state'] == 'aligned'


def pdf_bytes(pages):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    font = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    pdfmetrics.registerFont(TTFont('ProjectTest', font))
    out = io.BytesIO()
    c = canvas.Canvas(out)
    for text in pages:
        c.setFont('ProjectTest', 10)
        y = 810
        for line in text.splitlines():
            c.drawString(30, y, line)
            y -= 16
        c.showPage()
    c.save()
    return out.getvalue()


def pdf_doc(context, pages):
    obj = context.db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == context.org_id))
    return save_document(context.db, context.storage, org_id=context.org_id, object_id=obj.id, kind='project_doc',
        title='Книга чертежей', data={}, file_bytes=pdf_bytes(pages), file_name='project.pdf', created_by='test:project')


def test_incremental_pdf_provenance_and_explicit_element_link(context):
    doc = pdf_doc(context, ['Шифр РД: 17-2026-ЭМ\nЛист 3\nУчасток 123. Линия КЛ-1',
        'Шифр РД: 17-2026-ЭМ\nЛист 7\nПоз. | Наименование | Марка | Ед. | Кол-во | Линия\n1 | Кабель | ВВГнг-LS 3х2,5 | м | 100 | КЛ-1'])
    first = reader.read(context, doc.id, budget=1)
    assert first.state == 'partial' and first.payload['coverage']['remaining'] == 1
    second = reader.read(context, doc.id, budget=1)
    assert second.id == first.id and second.state == 'ready'
    rows = alignment.design_items([second])
    assert rows[0]['page'] == 2 and rows[0]['sheet'] == '7' and rows[0]['location'] == 'Участок 123'
    assert rows[0]['scope_method'] == 'explicit_element_link'
    assert rows[0]['scope_evidence'][0]['page'] == 1
    assert context.db.scalar(select(func.count()).select_from(ProjectReading)) == 1


def test_duplicate_element_across_areas_does_not_inherit_arbitrary_location(context):
    doc = pdf_doc(context, ['Шифр РД: 17-2026-ЭМ\nУчасток 123. Линия КЛ-1',
        'Шифр РД: 17-2026-ЭМ\nУчасток 124. Линия КЛ-1',
        'Шифр РД: 17-2026-ЭМ\nПоз. | Наименование | Марка | Ед. | Кол-во | Линия\n1 | Кабель | ВВГнг-LS 3х2,5 | м | 100 | КЛ-1'])
    reading = reader.read(context, doc.id)
    item = alignment.design_items([reading])[0]
    assert item['location'] is None
    compared = alignment.compare(alignment.target('Прокладка кабеля', 'участок 123', ['кабель ВВГнг-LS 3х2,5'], element='КЛ-1'), [reading])
    assert compared['state'] == 'insufficient'


def test_blank_pdf_and_vision_description_do_not_prove_materials(context):
    from bis.rag.embeddings import get_embedder
    from bis.rag.ingest import ingest_text
    doc = pdf_doc(context, [''])
    src = ingest_text(context.db, get_embedder(), org_id=context.org_id, text=TABLE, kind='project_doc', title='AI description', document_id=doc.id)
    src.meta = {'vision': True}
    result = reader.read(context, doc.id)
    assert result.state == 'partial' and result.payload['coverage']['unread']
    assert not alignment.design_items([result])


def test_vision_only_observations_never_become_verified_text(context):
    from bis.llm.gateway import LLMGateway, ProviderEntry
    from bis.llm.providers.offline import ScriptedProvider
    provider = ScriptedProvider([json.dumps({'claims': [{'field': 'material', 'value': 'кабель ВВГ 3х95', 'quote': 'кабель ВВГ 3х95'}]})])
    context.gateway = LLMGateway(entries={'test': ProviderEntry(provider, 'confidential', capabilities=('text', 'vision'))})
    result = structure.read_page(context.gateway, '', images=[{'mime': 'image/png', 'data': 'AA=='}])
    assert not result['claims'] and result['observations'] and not result['complete']


def test_changed_source_keeps_old_reading_and_original(context, corpus):
    first = reader.read(context, corpus['project'].id)
    old = context.storage.get(first.file_key)
    replace_text(context, corpus['project'], TABLE)
    assert reader.current(context, corpus['project'].id) is None
    second = reader.read(context, corpus['project'].id)
    assert second.id != first.id
    assert context.storage.get(first.file_key) == old
    assert reader.current(context, corpus['project'].id).id == second.id


def test_forced_reread_stays_current_and_invalidates_prior_act(context, corpus):
    from bis.services.executive_pipeline import validate_sources
    event = run(context)
    doc = context.db.get(Document, event.document_id)
    old = doc.data['project_alignment']['readings'][0]['id']
    reread = reader.read(context, corpus['project'].id, force=True)
    assert str(reread.id) != old and reader.current(context, corpus['project'].id).id == reread.id
    with pytest.raises(ValueError, match='Структура прочитанного проекта'):
        validate_sources(context.db, context.storage, doc)


def test_pipeline_uses_reading_and_quantity_check(context, corpus):
    replace_text(context, corpus['project'], TABLE + '\nНа скрытую прокладку оформить АОСР.\nИспытания: измерение сопротивления изоляции\nДокументы качества: сертификат')
    event = run(context, 'Проложен кабель на участке 123, линия КЛ-1. Объем: 110 м.')
    assert any(g['field'] == 'project_quantity' for g in event.result['gaps']), event.result['gaps']
    assert event.result['plan']['project_alignment']['checks'][0]['candidates'][0]['position'] == '1'


def test_api_isolates_readings_and_cannot_be_written_by_viewer(client, auth, context, corpus):
    context.db.commit()
    pid = str(corpus['project'].object_id)
    headers = {**auth, 'X-BIS-Project': pid}
    response = client.post(f'/api/projects/documents/{corpus["project"].id}/read', headers=headers, json={'budget': 1})
    assert response.status_code == 200, response.text
    rid = response.json()['id']
    assert client.get(f'/api/projects/readings/{rid}/file', headers=headers).status_code == 200
    other = ConstructionObject(organization_id=context.org_id, name='Другой объект', code='OTHER-PROJECT')
    context.db.add(other)
    context.db.commit()
    foreign = {**auth, 'X-BIS-Project': str(other.id)}
    assert client.get(f'/api/projects/readings/{rid}', headers=foreign).status_code == 404
    assert client.post(f'/api/projects/documents/{corpus["project"].id}/read', headers=foreign, json={}).status_code == 404
    user = context.db.get(User, context.user.id)
    user.role = 'viewer'
    context.db.commit()
    assert client.post(f'/api/projects/documents/{corpus["project"].id}/read', headers=headers, json={}).status_code == 403


def test_generic_material_marks_and_dimensions_in_name_are_not_lost():
    assert alignment.material_matches_row({'name': 'Мембрана гидроизоляционная', 'mark': 'ТЕХНОЭЛАСТ ЭПП'}, 'мембрана ТЕХНОЭЛАСТ ЭПП')
    assert alignment.material_matches_row({'name': 'Арматура d12', 'mark': 'А500С'}, 'арматура А500С d12')
    assert not alignment.material_matches_row({'name': 'Арматура d12', 'mark': 'А500С'}, 'арматура А500С d16')


def test_numeric_cell_is_not_a_logical_sheet_number():
    claim = structure.DesignClaim(field='sheet', value='100', quote='Кабель ВВГнг-LS 3х2,5 м 100')
    assert not structure.valid_claim(claim, claim.quote)


def test_header_identifies_qty_before_unit_and_preserves_zero():
    result = structure.heuristic_page('Поз. | Наименование | Марка | Количество | Ед.\n1 | Материал | М-1 | 0 | кг')
    assert result.items[0].quantity == '0' and result.items[0].unit == 'кг'


def test_comparison_uses_edited_act_fields_not_old_alignment(context, corpus):
    from bis.services.executive_pipeline import validate_sources
    event = run(context)
    act = context.db.get(Document, event.document_id)
    act.data = {**act.data, 'materials': ['кабель ВВГ 3х95']}
    checked = alignment.compare_act(context, act)
    assert checked['state'] == 'conflict'
    assert checked['checks'][0]['material'] == 'кабель ВВГ 3х95'
    with pytest.raises(ValueError, match='Сведения подготовленного акта изменились'):
        validate_sources(context.db, context.storage, act)


def test_pd_stage_does_not_become_working_design_basis(context):
    doc = source(context, 'project_doc', 'Стадия П', 'Стадия: П\n' + TABLE)
    reading = reader.read(context, doc.id)
    result = alignment.compare(alignment.target('Прокладка кабеля', 'участок 123', ['кабель ВВГнг-LS 3х2,5'], element='КЛ-1'), [reading])
    assert result['state'] == 'insufficient'


def test_reading_updates_revision_per_sheet_without_dropping_other_sheets(context):
    pages = [TABLE.replace('Лист 7; Изм. 1', 'Лист 7; Изм. 1'),
             TABLE.replace('Лист 7; Изм. 1', 'Лист 8; Изм. 1'),
             TABLE.replace('Лист 7; Изм. 1', 'Лист 7; Изм. 2').replace('| 100 |', '| 120 |')]
    doc = pdf_doc(context, pages)
    reading = reader.read(context, doc.id)
    rows = alignment.design_items([reading])
    assert {(r['sheet'], r['revision']) for r in rows} == {('7', '2'), ('8', '1')}
    assert any(r['quantity'] == '120' and r['sheet'] == '7' for r in rows)


def test_pdf_table_continuation_retains_actual_header_page(context):
    first = 'Шифр РД: 17-2026-ЭМ\nПоз. | Наименование | Марка | Ед. | Кол-во\n1 | Кабель | ВВГнг-LS 3х2,5 | м | 100'
    second = 'Продолжение спецификации\n2 | Кабель | ВВГнг-LS 3х25 | м | 250'
    doc = pdf_doc(context, [first, second])
    reading = reader.read(context, doc.id)
    row = reading.payload['pages'][1]['items'][0]
    assert row['quantity'] == '250' and row['header_page'] == 1
    assert reading.payload['pages'][1]['physical_page'] == 2


def test_reference_sheet_does_not_replace_own_title_block():
    page = structure.heuristic_page('Шифр РД: 17-2026-ЭМ\nЛист 3\nСм. 17-2026-ЭМ, лист 7.')
    assert [c.value for c in page.claims if c.field == 'sheet'] == ['3']
    assert any(c.field == 'reference' and 'лист 7' in c.value for c in page.claims)


def test_large_excel_continues_header_with_bounded_rows(context):
    import openpyxl
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(['Поз.', 'Наименование', 'Марка', 'Ед.', 'Кол-во'])
    for i in range(205):
        sheet.append([str(i+1), 'Кабель', 'ВВГнг-LS 3х2,5', 'м', 10])
    raw = io.BytesIO()
    book.save(raw)
    obj = context.db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == context.org_id))
    doc = save_document(context.db, context.storage, org_id=context.org_id, object_id=obj.id, kind='project_doc', title='Большая таблица',
        data={}, file_bytes=raw.getvalue(), file_name='large.xlsx', created_by='test:project')
    reading = reader.read(context, doc.id, budget=1)
    assert reading.payload['coverage']['remaining'] == 1
    assert len(reading.payload['pages'][0]['items']) == 199
    reading = reader.read(context, doc.id, budget=1)
    assert reading.state == 'ready'
    assert len(reading.payload['pages'][1]['items']) == 6


def test_missing_formula_result_is_not_zero_quantity():
    result = structure.read_page(None, 'Поз. | Наименование | Марка | Ед. | Кол-во\n1 | Кабель | ВВГнг-LS 3х2,5 | м | ')
    assert not result['complete']
    assert result['items'][0]['quantity'] is None


def test_element_clarification_resumes_same_draft_with_audit(context, corpus):
    replace_text(context, corpus['project'], TABLE.replace('3х25', '3х2,5') + '\nНа скрытую прокладку кабеля оформить АОСР.\nИспытания: измерение сопротивления изоляции\nДокументы качества: сертификат')
    event = run(context)
    assert event.result['plan']['project_alignment']['state'] == 'ambiguous'
    draft = event.document_id
    event.result = {**event.result, 'revision': 2, 'resolutions': {'element': 'КЛ-1'}}
    from bis.services.executive_pipeline import process_run
    process_run(context, event)
    assert event.document_id == draft
    assert event.result['plan']['project_alignment']['state'] == 'aligned', event.result['gaps']
    act = context.db.get(Document, draft)
    assert 'элемент: КЛ-1' in act.data['works']
    assert any(e['field'] == 'element' and e['method'] == 'user_correction' for e in act.data['evidence'])


def test_instruction_verb_is_not_a_cable_grade():
    import datetime as dt

    from bis.services.executive_pipeline import heuristic_report
    assert not structure.heuristic_page('На скрытую прокладку кабеля оформить АОСР.').items
    assert not heuristic_report('Кабель проложен на участке 123.', dt.date.today()).materials
    assert structure.heuristic_page('Материалы: кабель ввгнг-ls 3х2,5').items


def test_same_cable_from_report_and_journal_is_one_material_for_quantity():
    scoped = alignment.target('Объем: 90 м.', 'участок 123', ['кабель ВВГнг-LS 3х2,5', 'кабеля ВВГнг-LS 3х2,5'])
    assert len(scoped['materials']) == 1


def test_native_check_reuses_prior_visual_reading_without_discarding_it(context, corpus):
    first = reader.read(context, corpus['project'].id, with_vision=True)
    second = reader.read(context, corpus['project'].id)
    assert second.id == first.id and reader.current(context, corpus['project'].id).id == first.id


def test_worker_failures_do_not_starve_unread_valid_project_files(context):
    import datetime as dt

    from bis.models import Organization
    from bis.services.project_reading_jobs import refresh
    for i in range(5):
        bad = source(context, 'project_doc', 'Неподдерживаемый ' + str(i), 'Участок 123. Кабель ВВГнг-LS 3х2,5.')
        bad.file_name = 'unsupported.xyz'
        bad.mime = 'application/octet-stream'
        bad.updated_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=5-i)
    good = source(context, 'project_doc', 'Корректная РД', TABLE)
    context.db.commit()
    org = context.db.get(Organization, context.org_id)
    first = refresh(context.db, org, dt.date.today())
    assert first['failed'] == 5 and first['documents'] == 0
    second = refresh(context.db, org, dt.date.today())
    assert second['documents'] == 1
    assert context.db.get(Document, good.id).data.get('project_reading_id')


def test_worker_does_not_wait_to_write_failure_on_locked_project(context):
    import datetime as dt
    import threading
    import time

    from bis.db import SessionLocal
    from bis.models import Organization
    from bis.services.project_reading_jobs import refresh
    doc = source(context, 'project_doc', 'РД для параллельного чтения', TABLE)
    context.db.commit()
    org = context.db.get(Organization, context.org_id)
    with SessionLocal() as locker:
        locker.scalar(select(Document.id).where(Document.id == doc.id).with_for_update())
        release = threading.Timer(5, locker.rollback)
        release.start()
        started = time.monotonic()
        try:
            result = refresh(context.db, org, dt.date.today())
        finally:
            release.cancel()
        assert time.monotonic() - started < 4
        assert result['failed'] == 1 and result['documents'] == 0
        assert 'project_reading_error' not in context.db.get(Document, doc.id).data
