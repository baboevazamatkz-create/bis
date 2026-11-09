"""End-to-end evidence and lifecycle checks for the report → ID package pipeline."""

import datetime as dt
import io
import json
import uuid
import zipfile

import pytest
from sqlalchemy import func, select

from bis.api.deps import agent_context
from bis.models import ConstructionObject, Document, IntakeEvent, MaterialReceipt, MaterialUsage, User
from bis.rag.embeddings import get_embedder
from bis.rag.ingest import ingest_text
from bis.services import executive_pipeline as p
from bis.services.documents import save_document


@pytest.fixture
def context(db, demo):
    user = db.scalar(select(User).where(User.organization_id == demo.id, User.role == "admin"))
    ctx = agent_context(db, user)
    ctx.gateway = None
    ctx.workspace = "executive"
    ctx.focus = {}
    return ctx


def source(ctx, kind, title, text, *, location_object=True):
    obj = ctx.db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == ctx.org_id))
    doc = save_document(ctx.db, ctx.storage, org_id=ctx.org_id, kind=kind, title=title, data={}, file_bytes=text.encode(),
        file_name=f"{kind}.txt", created_by="test:pipeline", object_id=obj.id if location_object else None,
        doc_date=dt.date(2026, 1, 1), mime="text/plain")
    doc.status = "registered"
    ingest_text(ctx.db, get_embedder(), org_id=ctx.org_id, text=text, kind="org_document", title=title, document_id=doc.id)
    return doc


@pytest.fixture
def corpus(context):
    project = source(context, "project_doc", "РД участка 123", """Рабочая документация. Участок 123, прокладка кабеля.
Шифр РД: ЭМ-123, лист 7
Материалы: кабель ВВГнг-LS 3х2,5
На скрытую прокладку оформить АОСР.
Документы качества: сертификат
Испытания: измерение сопротивления изоляции
Последующие работы: обратная засыпка траншеи
Нормы: СП 76.13330.2016"""
    )
    journal = source(context, "work_journal", "Общий журнал работ участка 123", """01.11.2026 участок 123 начата прокладка кабеля ВВГнг-LS 3х2,5.
05.11.2026 участок 123 завершена прокладка кабеля ВВГнг-LS 3х2,5.
01.10.2026 участок 1234 начата прокладка кабеля ВВГнг-LS 3х2,5.
02.10.2026 участок 1234 завершена прокладка кабеля ВВГнг-LS 3х2,5.""")
    cert = source(context, "certificate", "Сертификат С-1", "Сертификат № С-1 на кабель ВВГнг-LS 3х2,5; действителен до 31.12.2026.")
    scheme = source(context, "scheme", "Исполнительная схема участка 123", "Участок 123. Исполнительная схема прокладки кабеля ВВГнг-LS 3х2,5.")
    protocol = source(context, "test_protocol", "Протокол испытаний участка 123", "Участок 123. Измерение сопротивления изоляции кабеля ВВГнг-LS 3х2,5. Результаты соответствуют.")
    return {"project": project, "journal": journal, "cert": cert, "scheme": scheme, "protocol": protocol}


def run(ctx, text="Проложен кабель на участке 123."):
    event, _ = p.create_run(ctx, p.RunInput(text=text))
    p.process_run(ctx, event)
    ctx.db.flush()
    return event


def test_complete_report_builds_grounded_draft_and_real_attachments(context, corpus):
    before = {model: context.db.scalar(select(func.count()).select_from(model)) for model in (MaterialReceipt, MaterialUsage)}
    event = run(context)
    assert event.result["status"] == "ready_for_review", event.result["gaps"]
    doc = context.db.get(Document, event.document_id)
    assert doc.status == "draft" and doc.kind == "aosr"
    assert doc.data["work_start"] == "2026-11-01" and doc.data["work_end"] == "2026-11-05"
    assert "ЭМ-123" in doc.data["project_docs"] and "засыпка" in doc.data["next_works"]
    assert {a["id"] for a in event.result["attachments"]} == {str(corpus[k].id) for k in ("project", "cert", "scheme", "protocol")}
    assert any(e["field"] == "work_start" and e["document_id"] == str(corpus["journal"].id) for e in event.result["evidence"])
    assert all(context.db.scalar(select(func.count()).select_from(model)) == count for model, count in before.items())
    package = zipfile.ZipFile(io.BytesIO(p.bundle(context, event)))
    assert len(package.namelist()) == 6 and "manifest.json" in package.namelist()
    assert any(name.endswith(".docx") for name in package.namelist())


def test_project_material_does_not_prove_actual_installation(context, corpus):
    context.db.delete(corpus["journal"])
    context.db.flush()
    event = run(context)
    fields = {g["field"] for g in event.result["gaps"]}
    assert {"materials", "work_start", "work_end"} <= fields
    assert event.result["plan"]["planned_materials"]
    assert event.result["plan"]["materials"] == []
    assert context.db.get(Document, event.document_id).data["work_end"] is None


def test_wrong_section_is_not_a_fallback_for_dates_or_scheme(context, corpus):
    corpus["scheme"].object_id = None
    corpus["scheme"].status = "replaced"
    event = run(context, "Проложен кабель на участке 124.")
    assert event.result["status"] == "needs_data"
    assert event.result["plan"]["work_start"] is None
    assert str(corpus["scheme"].id) not in {a["id"] for a in event.result["attachments"]}


def test_duplicate_certificates_are_concrete_choices_and_resume_does_not_duplicate_act(context, corpus):
    duplicate = source(context, "certificate", "Сертификат С-2", "Сертификат № С-2 на кабель ВВГнг-LS 3х2,5; действителен до 31.12.2026.")
    event = run(context)
    original_id = event.document_id
    quality_gap = next(g for g in event.result["gaps"] if g["field"].startswith("quality:"))
    assert {c["id"] for c in quality_gap["candidates"]} == {str(duplicate.id), str(corpus["cert"].id)}
    event.result = {**event.result, "resolutions": {"choices": {quality_gap["field"]: str(corpus["cert"].id)}}}
    p.process_run(context, event)
    assert event.result["status"] == "ready_for_review", event.result["gaps"]
    assert event.document_id == original_id
    assert str(duplicate.id) not in {a["id"] for a in event.result["attachments"]}


def test_expired_certificate_blocks_package(context, corpus):
    source(context, "certificate", "Просроченный сертификат", "Сертификат на кабель ВВГнг-LS 3х2,5; действителен до 01.01.2025.")
    corpus["cert"].status = "replaced"
    event = run(context)
    assert any(g["field"].startswith("quality:") for g in event.result["gaps"])


def test_changed_source_blocks_bundle_and_approval(context, corpus):
    from bis.services.documents import approve_document

    event = run(context)
    corpus["cert"].file_key = context.storage.put(b"different material", "changed.txt", prefix="tests")
    with pytest.raises(ValueError, match="изменился"):
        p.bundle(context, event)
    with pytest.raises(ValueError, match="изменился"):
        approve_document(context.db, context.db.get(Document, event.document_id), context.user)


def test_identity_does_not_match_larger_cross_section():
    assert p.material_matches("кабель ВВГнг-LS 3х2,5", "Сертификат на кабель ВВГнг-LS 3x2.5;")
    assert not p.material_matches("кабель ВВГ 3х2", "кабель ВВГ 3х25;")
    assert not p.scope_matches("участок 1234", "участок 123")


def test_hallucinated_model_fact_is_rejected(context, corpus):
    from bis.llm.gateway import LLMGateway, ProviderEntry
    from bis.llm.providers.offline import ScriptedProvider

    llm = ScriptedProvider([json.dumps({"facts": [{"field": "material", "value": "кабель ВВГ 3х95", "quote": "несуществующая цитата"}]})])
    context.gateway = LLMGateway(entries={"model": ProviderEntry(llm, "confidential")})
    facts, complete, _ = p.source_facts(context, corpus["project"], p.document_text(context.db, corpus["project"]))
    assert not complete and not any("3х95" in f.value for f in facts.facts)


def test_idempotency_key_cannot_silently_change_input(context):
    body = p.RunInput(text="Проложен кабель на участке 123")
    first, fresh = p.create_run(context, body)
    second, again = p.create_run(context, body)
    assert fresh and not again and first.id == second.id
    with pytest.raises(ValueError, match="другого сообщения"):
        p.create_run(context, body.model_copy(update={"text": "Проложен кабель на участке 124"}))


def test_planned_work_does_not_create_act(context, corpus):
    event = run(context, "Планируем проложить кабель на участке 123")
    assert event.document_id is None and any(g["field"] == "completion" for g in event.result["gaps"])


def test_api_background_job_persists_result_and_prevents_stale_resume(client, auth, context, corpus):
    context.db.commit()
    key = str(uuid.uuid4())
    r = client.post("/api/executive/runs", headers=auth, json={"text": "Проложен кабель на участке 123.", "request_id": key})
    assert r.status_code == 202, r.text
    result = client.get(f"/api/executive/runs/{key}", headers=auth).json()
    assert result["status"] == "ready_for_review", result
    assert client.post("/api/executive/runs", headers=auth, json={"text": "Проложен кабель на участке 123.", "request_id": key}).status_code == 202
    assert client.post(f"/api/executive/runs/{key}/resume", headers=auth, json={"revision": 999}).status_code == 409
    download = client.get(f"/api/executive/runs/{key}/bundle", headers=auth)
    assert download.status_code == 200 and download.content[:2] == b"PK"


def test_date_conflict_remains_explicit_instead_of_using_latest_date(context, corpus):
    event = run(context, "Проложен кабель на участке 123. Монтаж закончили 06.11.2026.")
    assert any(g["field"] == "conflict:work_end" for g in event.result["gaps"])
    assert event.result["plan"]["work_end"] is None


def test_report_detection_does_not_turn_a_question_into_a_write():
    assert p.is_work_report("На участке 123 проложен кабель")
    assert not p.is_work_report("Когда проложен кабель на участке 123?")
    assert not p.is_work_report("Выполнение прокладки кабеля на участке 123")
    assert p.is_work_report("Монтаж кабеля на участке 123 закончен")


def test_complementary_project_sheets_are_combined_without_unnecessary_question(context, corpus):
    extra = source(context, "project_doc", "Спецификация участка 123", "Участок 123. Прокладка кабеля.\nШифр РД: ЭМ-123, лист 8\nМатериалы: кабель ВВГнг-LS 3х2,5")
    event = run(context)
    assert not any(g["field"] == "project" for g in event.result["gaps"])
    assert "лист 7" in event.result["plan"]["project_docs"] and "лист 8" in event.result["plan"]["project_docs"]
    assert str(extra.id) in {a["id"] for a in event.result["attachments"]}


def test_future_revision_does_not_replace_project_effective_at_work_date(context, corpus):
    corpus["project"].data = {"card": {"rd_code": "ЭМ-123", "revision": "1"}}
    newer = source(context, "project_doc", "РД участка 123 изменение 2", "Участок 123. Прокладка кабеля.\nШифр РД: ЭМ-123, лист 7\nМатериалы: кабель ВВГ 3х95")
    newer.date, newer.data = dt.date(2026, 12, 1), {"card": {"rd_code": "ЭМ-123", "revision": "2"}}
    event = run(context)
    assert str(newer.id) not in {a["id"] for a in event.result["attachments"]}
    assert not any(g["field"] == "material_conflict" for g in event.result["gaps"])


def test_source_cache_reuses_extraction_and_invalidates_after_model_change(context, corpus):
    from bis.llm.gateway import LLMGateway, ProviderEntry
    from bis.llm.providers.offline import ScriptedProvider

    llm = ScriptedProvider(['{"facts": []}', '{"facts": []}'])
    context.gateway = LLMGateway(entries={"model": ProviderEntry(llm, "confidential")})
    text = p.document_text(context.db, corpus["project"])
    first = p.source_facts(context, corpus["project"], text)
    again = p.source_facts(context, corpus["project"], text)
    assert first[0] == again[0] and len(llm.calls) == 1
    llm.model = "new-model"
    p.source_facts(context, corpus["project"], text)
    assert len(llm.calls) == 2


def test_voice_route_preserves_recording_and_deduplicates_transcription(client, auth, context, corpus, monkeypatch):
    calls = []
    monkeypatch.setattr("bis.voice.transcribe", lambda *args: calls.append(True) or "Проложен кабель на участке 123.")
    context.db.commit()
    key = str(uuid.uuid4())
    kwargs = {"headers": auth, "data": {"request_id": key}, "files": {"file": ("voice.ogg", b"fake audio", "audio/ogg")}}
    assert client.post("/api/executive/runs/voice", **kwargs).status_code == 202
    assert client.post("/api/executive/runs/voice", **kwargs).status_code == 202
    assert len(calls) == 1
    context.db.expire_all()
    event = context.db.get(IntakeEvent, uuid.UUID(key))
    assert event.kind == "voice" and context.storage.get(event.file_key) == b"fake audio"
    assert event.result["status"] == "ready_for_review", event.result


def test_queued_run_is_recovered_by_worker_after_restart(context, corpus):
    from bis.models import Organization
    from bis.services.executive_jobs import recover

    event, _ = p.create_run(context, p.RunInput(text="Проложен кабель на участке 123."))
    context.db.commit()
    result = recover(context.db, context.db.get(Organization, context.org_id), dt.date.today())
    context.db.refresh(event)
    assert result["resumed"] == 1 and event.result["status"] == "ready_for_review"


def test_abandoned_attempt_is_fenced_and_can_be_retried(context):
    from bis.models import Organization
    from bis.services.executive_jobs import recover

    event, _ = p.create_run(context, p.RunInput(text="Проложен кабель на участке 123."))
    event.result = {**event.result, "status": "running", "attempt": "old-attempt", "started_at": (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()}
    context.db.commit()
    result = recover(context.db, context.db.get(Organization, context.org_id), dt.date.today())
    context.db.refresh(event)
    assert result["abandoned"] == 1 and event.result["attempt"] is None and event.result["status"] == "error"


def test_chat_report_uses_same_pipeline_without_high_level_model_tool_choice(client, auth, context, corpus):
    context.db.commit()
    r = client.post("/api/chat", headers=auth, json={"message": "Проложен кабель на участке 123.", "workspace": "executive"})
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "executive_pipeline"
    assert r.json()["documents"] and r.json()["trace"][0]["tool"] == "prepare_executive_package"
    result = client.get("/api/executive/runs", headers=auth).json()
    assert len(result) == 1 and result[0]["status"] == "ready_for_review"


def test_approved_act_cannot_be_rewritten_by_resume(client, auth, context, corpus):
    event = run(context)
    context.db.commit()
    assert client.post(f"/api/documents/{event.document_id}/approve", headers=auth).status_code == 200
    assert client.post(f"/api/executive/runs/{event.id}/resume", headers=auth, json={"revision": 1}).status_code == 409


def test_pipeline_endpoints_enforce_viewer_write_permissions(client, auth):
    from conftest import login

    client.post("/api/users", headers=auth, json={"email": "viewer-pipeline@demo.bis", "full_name": "Наблюдатель", "role": "viewer", "password": "viewer123"})
    viewer = login(client, "viewer-pipeline@demo.bis", "viewer123")
    assert client.post("/api/executive/runs", headers=viewer, json={"text": "Проложен кабель на участке 123"}).status_code == 403


def test_foreign_organization_cannot_read_run(client, db, context):
    from conftest import login

    from bis.demo import create_organization

    event, _ = p.create_run(context, p.RunInput(text="Проложен кабель на участке 123"))
    create_organization(db, name="Другая", inn="1435990003", admin_email="other-pipeline@demo.bis", admin_password="12345678", admin_name="Админ")
    db.commit()
    other = login(client, "other-pipeline@demo.bis", "12345678")
    assert client.get(f"/api/executive/runs/{event.id}", headers=other).status_code == 404
    assert client.get(f"/api/executive/runs/{event.id}/bundle", headers=other).status_code == 404


@pytest.mark.parametrize('conclusion', ['Результаты не соответствуют требованиям.', 'Измеренные значения: 0,1 МОм.'])
def test_protocol_without_positive_conclusion_blocks_package(context, corpus, conclusion):
    context.db.delete(corpus['protocol'])
    context.db.flush()
    source(context, 'test_protocol', 'Протокол участка 123',
           'Участок 123. Измерение сопротивления изоляции кабеля ВВГнг-LS 3х2,5. ' + conclusion)
    event = run(context)
    assert event.result['status'] == 'needs_data'
    assert any(g['field'].startswith('protocol_result:') for g in event.result['gaps'])
    assert any(i['level'] == 'error' and i['field'].startswith('protocol_result:')
               for i in context.db.get(Document, event.document_id).issues)


def test_protocol_does_not_use_another_sections_positive_conclusion():
    text = 'Участок 123: значения измерены.\nУчасток 124: результаты соответствуют.'
    assert p.protocol_conclusion(text, 'участок 123')[0] == 'unknown'
    assert p.protocol_conclusion('Результаты не соответствуют. Другие результаты соответствуют.', 'участок 123')[0] == 'failed'


def test_extraction_keeps_original_date_in_verbatim_quote():
    from bis.llm.extract import normalize_answer

    data = {'facts': [{'field': 'work_end', 'value': '05.11.2026', 'quote': '05.11.2026'}]}
    result = normalize_answer(data, p.Facts)
    assert result['facts'][0]['quote'] == data['facts'][0]['quote']
    assert result['facts'][0]['value'] == '2026-11-05'


def test_imported_certificate_expiry_is_not_used_as_issue_date(context, corpus):
    from bis.services.archive import import_file

    result = import_file(context.db, context.storage, None, get_embedder(), context.org_id,
                         data='Сертификат № С-2 на кабель ВВГнг-LS 3х2,5; действителен до 31.12.2026.'.encode(),
                         name='Сертификат С-2.txt', mime='text/plain', analyze_contracts=False)
    assert result['kind'] == 'certificate' and result['date'] is None
    event = run(context)
    assert any(len(g['candidates']) == 2 for g in event.result['gaps'] if g['field'].startswith('quality:'))
