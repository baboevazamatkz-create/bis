"""Acceptance scenarios: grounded requirements, relational checks and changing source versions."""
import datetime as dt
import json

import pytest
from sqlalchemy import select
from test_executive_pipeline import context as context  # noqa: F401
from test_executive_pipeline import corpus as corpus  # noqa: F401
from test_executive_pipeline import run, source
from test_libraries import library as library  # noqa: F401
from test_libraries import synchronize
from test_project_facts import replace_text

from bis.demo.executive_fixtures import generate
from bis.models import ConstructionObject, KnowledgeSource
from bis.services import executive_pipeline as pipeline
from bis.services.executive_readiness import review
from bis.services.executive_requirements import modality


def add_rule(ctx, library, text, *, status='active'):
    conn, root = library
    (root / 'Учебный регламент кабеля.txt').write_text(text, encoding='utf-8')
    synchronize(ctx.db, conn)
    item = ctx.db.scalar(select(KnowledgeSource).where(KnowledgeSource.title == 'Учебный регламент кабеля'))
    item.status = status
    ctx.db.flush()
    return item


def test_matrix_points_to_required_document_and_original_clause(context, corpus):
    event = run(context)
    assert event.result['status'] == 'ready_for_review', event.result['gaps']
    result = review(context, event)
    assert result['state'] == 'ready_for_review'
    requirements = result['requirements']
    assert {r['field'] for r in requirements} >= {'quality_requirement', 'protocol_requirement', 'act_kind'}
    protocol = next(r for r in requirements if r['field'] == 'protocol_requirement')
    assert protocol['quote'] == 'Испытания: измерение сопротивления изоляции'
    assert protocol['document_ids'] == [str(corpus['protocol'].id)]
    assert result['source_documents'] == 2


def test_normative_folder_is_read_without_internet_and_clause_inherits_single_work(context, corpus, library):
    rule = add_rule(context, library, 'Учебная прокладка кабеля.\nИспытания: проверка целостности кабеля')
    event = run(context)
    assert any('проверка целостности' in r['value'] and r['source'].get('source_id') == str(rule.id)
        for r in event.result['plan']['requirement_matrix']['items'])
    assert any(g['field'].startswith('protocol:') for g in event.result['gaps'])
    assert event.result['status'] == 'needs_data'


def test_conditional_rule_is_not_silently_applied_or_ignored(context, corpus, library):
    add_rule(context, library, 'Учебная прокладка кабеля.\nИспытания: если участок в особой зоне, требуется измерение сопротивления изоляции кабеля')
    event = run(context)
    rows = event.result['plan']['requirement_matrix']['items']
    assert any(r['state'] == 'conditional' for r in rows)
    assert any(g['field'].startswith('rule_condition:') for g in event.result['gaps'])


def test_opposite_requirements_require_resolution_in_sources(context, corpus, library):
    add_rule(context, library, 'Учебная прокладка кабеля.\nДокументы качества: сертификат не требуется')
    event = run(context)
    assert any(g['field'].startswith('rule_conflict:') for g in event.result['gaps'])
    assert any(r['state'] == 'conflict' for r in event.result['plan']['requirement_matrix']['items'])


def test_unconfirmed_normative_edition_blocks_readiness(context, corpus, library):
    add_rule(context, library, 'Учебная прокладка кабеля.\nДокументы качества: сертификат', status='unknown')
    event = run(context)
    assert any(g['field'].startswith('rules_unconfirmed:') for g in event.result['gaps'])


def test_journal_change_invalidates_act_even_without_attachment_change(context, corpus):
    event = run(context)
    replace_text(context, corpus['journal'], '06.11.2026 участок 123 завершена прокладка кабеля ВВГнг-LS 3х2,5.')
    result = review(context, event)
    assert result['state'] == 'blocked' and any(b['field'] == 'source_changed' for b in result['blockers'])
    with pytest.raises(ValueError, match='Источник изменился'):
        pipeline.bundle(context, event)


def test_new_normative_version_invalidates_prior_requirement_matrix(context, corpus, library):
    add_rule(context, library, 'Учебная прокладка кабеля.\nДокументы качества: сертификат')
    event = run(context)
    assert review(context, event)['state'] == 'ready_for_review'
    (library[1] / 'Учебный регламент кабеля.txt').write_text('Учебная прокладка кабеля.\nИспытания: проверка целостности кабеля', encoding='utf-8')
    synchronize(context.db, library[0])
    assert review(context, event)['state'] == 'blocked'


def test_new_rd_revision_requires_rebuild_but_other_sheet_does_not(context, corpus):
    corpus['project'].data = {'card': {'rd_code': 'ЭМ-123', 'rd_sheet': '7', 'revision': '1'}}
    event = run(context)
    sibling = source(context, 'project_doc', 'РД лист 8 изменение 2', 'Участок 123. Прокладка кабеля. Шифр РД: ЭМ-123, лист 8')
    sibling.data = {'card': {'rd_code': 'ЭМ-123', 'rd_sheet': '8', 'revision': '2'}}
    sibling.date = dt.date(2026, 11, 4)
    assert review(context, event)['state'] == 'ready_for_review'
    sibling.data = {'card': {'rd_code': 'ЭМ-123', 'rd_sheet': '7', 'revision': '2'}}
    assert review(context, event)['state'] == 'blocked'


@pytest.mark.parametrize('case', ['complete', 'missing_protocol', 'wrong_batch', 'wrong_material', 'conflicting_dates'])
def test_portable_archive_exercises_real_pipeline(context, tmp_path, case):
    generate(tmp_path / 'archive')
    folder = tmp_path / 'archive' / case
    scenario = json.loads((folder / 'scenario.json').read_text())
    for item in scenario['documents']:
        source(context, item['kind'], item['title'], (folder / item['file']).read_text())
    event = run(context, scenario['report'])
    if case == 'complete':
        assert event.result['status'] == 'ready_for_review', event.result['gaps']
    else:
        assert event.result['status'] == 'needs_data'
        assert all(any(g['field'].startswith(prefix) for g in event.result['gaps']) for prefix in scenario['expected_blocker_prefixes']), event.result['gaps']


def test_readiness_api_hides_other_project_even_without_strict_libraries(client, auth, context, corpus):
    event = run(context)
    other = ConstructionObject(organization_id=context.org_id, code='ISOLATED', name='Другой объект')
    context.db.add(other)
    context.db.commit()
    response = client.get(f'/api/executive/runs/{event.id}/readiness', headers=auth)
    assert response.status_code == 200 and response.json()['requirements']
    response = client.get(f'/api/executive/runs/{event.id}/readiness', headers={**auth, 'X-BIS-Project': str(other.id)})
    assert response.status_code == 404


def test_fixture_generation_never_overwrites_existing_documents(tmp_path):
    generate(tmp_path / 'archive')
    with pytest.raises(ValueError, match='не перезаписываются'):
        generate(tmp_path / 'archive')
    assert modality('При необходимости выполняется проверка') == 'conditional'
    assert pipeline.material_matches('кабель ВВГ 3х2,5', 'Проверен кабель ВВГ 3х2,5.')
    assert not pipeline.material_matches('кабель ВВГ 3х2,5', 'Проверен кабель ВВГ 3х2,55.')


def test_protocol_date_before_installation_blocks_readiness(context, corpus):
    replace_text(context, corpus['protocol'], '01.10.2026 участок 123. Измерение сопротивления изоляции кабеля ВВГнг-LS 3х2,5. Результаты соответствуют.')
    event = run(context)
    assert any(g['field'].startswith('protocol_date:') for g in event.result['gaps'])
    assert any(r['state'] == 'invalid' for r in event.result['plan']['requirement_matrix']['items'])


def test_act_kind_cannot_override_unconditional_rd_requirement(context, corpus):
    event, _ = pipeline.create_run(context, pipeline.RunInput(text='Проложен кабель на участке 123.'))
    event.result = {**event.result, 'resolutions': {'act_kind': 'aook'}}
    pipeline.process_run(context, event)
    assert any(g['field'].startswith('act_requirement:') for g in event.result['gaps'])


def test_actual_card_sheets_are_used_for_revision_impacts(context, corpus):
    corpus['project'].data = {'card': {'rd_code': 'ЭМ-123', 'sheets': ['7', '8'], 'revision': '1'}}
    event = run(context)
    newer = source(context, 'project_doc', 'Изменение листа 8', 'Участок 123. Прокладка кабеля. Шифр РД: ЭМ-123, лист 8')
    newer.data = {'card': {'rd_code': 'ЭМ-123', 'sheets': ['8'], 'revision': '2'}}
    newer.date = dt.date(2026, 11, 4)
    assert review(context, event)['state'] == 'blocked'


def test_generic_condition_is_not_an_area_identifier():
    assert not pipeline.locations('если участок в особой зоне')
    assert pipeline.locations('Участок В') == {'участ:в'}
