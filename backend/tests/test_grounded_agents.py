"""Behavioral regressions: evidence first, complete reading, shared project context."""

from __future__ import annotations

import io
import json

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import select

from bis.agents.base import AgentContext, bounded_json
from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
from bis.llm.providers.offline import ScriptedProvider
from bis.llm.types import LLMError, Message, ToolCall
from bis.models import Contract, Document, KnowledgeChunk, KnowledgeSource, User


def context(db, org):
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    user = db.scalar(select(User).where(User.organization_id == org.id))
    return AgentContext(db=db, org_id=org.id, user=user, gateway=None,
                        embedder=get_embedder(), storage=get_storage())


def archive_doc(ctx, text, *, number="77", indexed=True):
    from bis.rag.ingest import ingest_text

    key = ctx.storage.put(text.encode(), "contract.txt", prefix="grounding-tests")
    doc = Document(organization_id=ctx.org_id, kind="contract", title=f"Договор № {number}", number=number,
                   file_key=key, file_name="contract.txt", mime="text/plain", data={}, issues=[],
                   source="archive", status="registered", created_by="test")
    ctx.db.add(doc)
    ctx.db.flush()
    if indexed:
        ingest_text(ctx.db, ctx.embedder, org_id=ctx.org_id, text=text, kind="contract", title=doc.title, document_id=doc.id)
    return doc


def test_tool_results_remain_json_and_report_omissions():
    data = {"documents": [{"id": str(i), "text": "Условие договора " * 3000} for i in range(30)]}
    result = json.loads(bounded_json(data))
    assert result["truncated"] is True and result["omitted"]
    assert result["result"]["documents"][0]["id"] == "0"
    assert len(json.dumps(result, ensure_ascii=False)) <= 24000


def test_all_specialists_can_read_cross_domain_data(db, demo):
    from bis.plugins.sdk import discover

    reg = discover()
    ctx = context(db, demo)
    required = {"find_contracts", "get_contract_details", "get_document", "document_card", "find_materials",
                "correspondence_status", "list_obligations", "list_tasks", "plan_fact", "data_coverage"}
    for agent in reg.agents.values():
        assert required <= {t.name for t in agent.available_tools(ctx, reg.tools)}, agent.name
    ctx.user.role = "viewer"
    for agent in reg.agents.values():
        assert not any(sc.endswith(":write") for t in agent.available_tools(ctx, reg.tools) for sc in t.scopes)


def test_document_middle_and_end_survive_tool_transport(db, demo):
    from bis.agents.tools_control import get_document

    ctx = context(db, demo)
    doc = archive_doc(ctx, "Вступление. " * 1500 + "\n6.7 Оплата в течение 17 рабочих дней.\n"
                      + "Прочие условия. " * 1500 + "\nРеквизиты: ИНН заказчика 7701234567.")
    content, error = get_document.run(ctx, {"document_id": str(doc.id), "query": "Оплата рабочих дней"})
    assert not error
    result = json.loads(content)
    assert not result.get("truncated") and not result["complete"]
    assert "7701234567" in result["ending"]
    assert any("17 рабочих дней" in h["text"] for h in result["found"])
    assert result["next_offset"] == 0
    # Sequential windows really cover the full text, including its middle.
    offset, pieces = 0, []
    while True:
        raw, error = get_document.run(ctx, {"document_id": str(doc.id), "offset": offset, "limit_chars": 3000})
        window = json.loads(raw)
        assert not error and window["offset"] == offset
        pieces.append(window["text"])
        if window["next_offset"] is None:
            break
        assert window["next_offset"] > offset
        offset = window["next_offset"]
    from bis.services.document_text import document_text

    assert "".join(pieces) == document_text(db, doc)


def test_contract_file_found_even_without_registry_row(db, demo):
    from bis.agents.tools_control import ContractArgs, get_contract_details

    ctx = context(db, demo)
    # A separate tenant containing only one archived contract, no registered contracts.
    from bis.demo import create_organization

    org, _ = create_organization(db, name="Пилот", inn="1435990001", admin_email="ground@pilot.ru",
                                 admin_password="12345678", admin_name="Админ")
    ctx = context(db, org)
    doc = archive_doc(ctx, "Договор № 77. Заказчик АО Север. Оплата в течение 17 дней.")
    result = get_contract_details.handler(ctx, ContractArgs())
    assert result["archive_only"] and result["document_id"] == str(doc.id)
    assert "17 дней" in result["text"]
    assert db.scalar(select(Contract).where(Contract.organization_id == org.id)) is None


def test_ambiguous_contracts_return_actual_choices(db, demo):
    from bis.agents.grounding import recovery_answer
    from bis.agents.tools_control import get_contract_details

    ctx = context(db, demo)
    db.add(Contract(organization_id=demo.id, number="ВТОРОЙ-77", kind="supply", our_role="customer", status="active"))
    db.flush()
    content, error = get_contract_details.run(ctx, {})
    data = json.loads(content)
    assert error and ctx.ambiguous and len(data["candidates"]) >= 2
    answer = recovery_answer(ctx)
    assert "ВТОРОЙ-77" in answer and "Какой из них" in answer


def test_single_contract_loaded_before_model_can_ask_for_number(client, auth):
    llm = ScriptedProvider(["Предоставьте номер договора.", "ИНН заказчика указан в реквизитах."])
    set_gateway(LLMGateway(entries={"local": ProviderEntry(llm, "confidential")}))
    response = client.post("/api/chat", headers=auth, json={"message": "Какие реквизиты заказчика по договору?"})
    assert response.status_code == 200
    initial = "\n".join(m.content for m in llm.calls[0]["messages"])
    assert "Договор № ГП-01/2026" in initial and '"read": [' in initial
    assert any(t.get("phase") == "retrieval" for t in response.json()["trace"])


def test_delegated_specialist_receives_history_and_collected_evidence(client, auth):
    llm = ScriptedProvider([
        Message("assistant", tool_calls=[ToolCall("a", "get_contract_details", {})]),
        Message("assistant", tool_calls=[ToolCall("b", "delegate_to_agent", {"agent": "contracts", "task": "Проверь условия"})]),
        "Условия проверены.", "Проверка завершена.",
    ])
    set_gateway(LLMGateway(entries={"local": ProviderEntry(llm, "confidential")}))
    r = client.post("/api/chat", headers=auth, json={"message": "Проверь условия для нашего проекта", "agent": "assistant"})
    assert r.status_code == 200
    specialist = llm.calls[2]["messages"]
    assert any(m.role == "user" and m.content == "Проверь условия для нашего проекта" for m in specialist)
    inherited = "\n".join(m.content for m in specialist if m.role == "system")
    assert '"tool": "get_contract_details"' in inherited and "ГП-01/2026" in inherited


def test_followup_keeps_selected_document(client, auth):
    from bis.llm.gateway import get_gateway

    llm = ScriptedProvider([Message("assistant", tool_calls=[ToolCall("a", "get_contract_details", {})]), "Договор найден."])
    set_gateway(LLMGateway(entries={"local": ProviderEntry(llm, "confidential")}))
    first = client.post("/api/chat", headers=auth, json={"message": "Покажи договор"}).json()
    llm.responses[:] = [Message("assistant", tool_calls=[ToolCall("b", "get_document", {})]), "Срок указан в договоре."]
    second = client.post("/api/chat", headers=auth, json={"session_id": first["session_id"], "message": "А какой у него срок?"})
    assert second.status_code == 200
    assert not any(t["error"] for t in second.json()["trace"])
    assert "Выбран в диалоге document" in get_gateway().entries["local"].provider.calls[-2]["messages"][0].content


def test_entire_text_used_for_cards_and_id_checks(db, demo):
    from bis.brain.build import document_text
    from bis.services.id_check import _text

    ctx = context(db, demo)
    doc = archive_doc(ctx, "", indexed=False)
    source = KnowledgeSource(organization_id=ctx.org_id, document_id=doc.id, kind="contract", title=doc.title)
    db.add(source)
    db.flush()
    for i in range(140):
        db.add(KnowledgeChunk(organization_id=ctx.org_id, source_id=source.id, ord=i,
                             content=f"Страница {i + 1}. " + ("Сертификат № 9999" if i == 110 else "Условия"), page=i + 1))
    db.flush()
    assert "Сертификат № 9999" in document_text(db, doc)
    assert "Сертификат № 9999" in _text(db, doc)


def test_failed_extraction_window_uses_fallback_instead_of_losing_tail():
    from bis.llm.extract import extract

    class Facts(BaseModel):
        facts: list[str] = Field(default_factory=list)

    class PartialProvider:
        name, model = "partial", "partial"

        def complete(self, messages, **kwargs):
            if "ХВОСТ" in messages[1].content:
                raise LLMError("Недоступно")
            return ScriptedProvider(['{"facts": ["Начало"]}']).complete(messages, **kwargs)

    gateway = LLMGateway(entries={"partial": ProviderEntry(PartialProvider(), "confidential")})
    result = extract(gateway, Facts, "Начало " * 300 + "\nХВОСТ", instructions="Извлеки факты", window_chars=1500,
                     fallback=lambda text: Facts(facts=["ХВОСТ"] if "ХВОСТ" in text else []))
    assert result.complete and result.used_fallback and "ХВОСТ" in result.data.facts
    assert result.windows_read == result.windows_total > 1


def test_ocr_does_not_drop_middle_pages_after_300():
    from pypdf import PdfReader, PdfWriter

    from bis.ocr import _pdf_pages

    writer = PdfWriter()
    for i in range(305):
        writer.add_blank_page(width=200 + i, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    pages = _pdf_pages(buf.getvalue())
    assert len(pages) == 305
    assert int(PdfReader(io.BytesIO(pages[220])).pages[0].mediabox.width) == 420


def test_card_model_reads_beyond_first_8000_characters():
    from bis.brain.card import build_card

    llm = ScriptedProvider(['{"summary": "Узел Фм9", "elements": ["Фм9"]}'])
    gateway = LLMGateway(entries={"test": ProviderEntry(llm, "confidential")})
    text = "Проектная документация. " * 500 + "\nКонструкция Фм9 в конце."
    card = build_card(gateway, text, "project_doc")
    assert "Фм9" in card.elements and "Конструкция Фм9 в конце." in llm.calls[0]["messages"][1].content


def test_missing_index_can_be_repaired(db, demo):
    from bis.brain.build import index_missing

    ctx = context(db, demo)
    doc = archive_doc(ctx, "Договор № 77. Срок оплаты 17 дней.", indexed=False)
    result = index_missing(db, None, ctx.embedder, ctx.storage, demo.id)
    assert result["indexed"] >= 1
    assert db.scalar(select(KnowledgeSource).where(KnowledgeSource.document_id == doc.id)).chunk_count > 0
    assert doc.data["card"]["number"] == "77"


def test_search_with_empty_source_scope_does_not_search_entire_archive(db, demo):
    from bis.rag.search import hybrid_search

    ctx = context(db, demo)
    assert hybrid_search(db, ctx.embedder, "договор", org_id=demo.id, source_ids=[]) == []


def test_foreign_focus_cannot_read_another_tenants_document(db, demo):
    from bis.agents.tools_control import get_document
    from bis.demo import create_organization

    other, _ = create_organization(db, name="Чужая организация", inn="1435990002", admin_email="other@pilot.ru",
                                   admin_password="12345678", admin_name="Админ")
    foreign = archive_doc(context(db, other), "Секретный договор другой организации.")
    ctx = context(db, demo)
    ctx.focus["document"] = str(foreign.id)
    content, error = get_document.run(ctx, {})
    assert error and "не найден" in json.loads(content)["error"].lower()
    assert "Секретный договор" not in content


def test_mixed_pdf_requires_ocr_and_keeps_native_text(monkeypatch):
    from bis.rag.parsers import parse_pdf
    from bis.services import archive

    native = "Условия договора в текстовом слое. " * 20

    class FakePage:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class FakeReader:
        pages = [FakePage(native), FakePage("")]
        metadata = None

    monkeypatch.setattr("pypdf.PdfReader", lambda data: FakeReader())
    parsed = parse_pdf(b"pdf")
    assert parsed.meta["needs_ocr"] and parsed.meta["ocr_pages"] == [2]
    monkeypatch.setattr(archive, "_ocr_pages", lambda data, mime: ["", "Приложение: оплата 17 дней."])
    pages, how = archive._raw_pages(b"pdf", "contract.pdf", "application/pdf")
    assert how == "ocr" and pages[0].text == native and "17 дней" in pages[1].text


def test_incomplete_ocr_does_not_replace_existing_index(db, demo, monkeypatch):
    from pypdf import PdfWriter

    from bis import ocr
    from bis.brain.build import reread_scan

    ctx = context(db, demo)
    doc = archive_doc(ctx, "Старый индекс, который нельзя потерять.")
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    doc.file_key = ctx.storage.put(buf.getvalue(), "scan.pdf", prefix="grounding-tests")
    doc.file_name, doc.mime = "scan.pdf", "application/pdf"
    monkeypatch.setattr(ocr, "recognize_pages", lambda *args: ["текст"] * 3)
    with pytest.raises(ocr.OCRError, match="3 страниц из 4"):
        reread_scan(db, None, ctx.embedder, ctx.storage, doc)
    assert not doc.data.get("pages_checked")
    from bis.services.document_text import document_text

    assert "нельзя потерять" in document_text(db, doc)


def test_vision_reads_more_than_eight_sheets_and_keeps_failed_page_number():
    from pypdf import PdfWriter

    from bis.brain.vision import read_sheets, sheet_images

    writer = PdfWriter()
    for _ in range(10):
        writer.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    writer.write(buf)
    images = sheet_images(buf.getvalue(), "application/pdf", "project.pdf")
    assert len(images) == 10
    sheet = json.dumps({"sheet_type": "план", "description": "План плиты"}, ensure_ascii=False)
    llm = ScriptedProvider([sheet, "invalid", "invalid", sheet])
    gateway = LLMGateway(entries={"vision": ProviderEntry(llm, "confidential", capabilities=("text", "vision"))})
    sheets = read_sheets(gateway, images[:3])
    assert [s.page_number for s in sheets] == [1, 3]


def test_duplicate_document_number_does_not_create_false_link(db, demo):
    from bis.brain.card import Card, DocRef
    from bis.brain.links import rebuild, resolve_pending
    from bis.models import DocumentLink

    ctx = context(db, demo)
    act = archive_doc(ctx, "АОСР № 8: документ о качестве № 9999.", number="8")
    act.kind = "aosr"
    first = archive_doc(ctx, "Документ о качестве № 9999.", number="9999")
    first.kind, first.title = "certificate", "Сертификат № 9999 первый"
    card = Card(kind="aosr", refs=[DocRef(kind="quality", number="9999")])
    act.data = {"card": card.model_dump()}
    db.flush()
    link, = rebuild(db, act, card)
    assert link.to_document_id == first.id
    duplicate = archive_doc(ctx, "Другой документ о качестве № 9999.", number="9999")
    duplicate.kind, duplicate.title = "certificate", "Сертификат № 9999 другой"
    db.flush()
    resolve_pending(db, duplicate)
    assert db.get(DocumentLink, link.id).to_document_id is None


def test_offline_contract_resolution_does_not_choose_first_of_many():
    from bis.llm.providers.offline import _contract_id

    previous = {"find_contracts": {"contracts": [
        {"id": "a", "number": "77", "kind": "general"}, {"id": "b", "number": "88", "kind": "subcontract"}]}}
    assert _contract_id("Покажи условия договора", previous) is None
    assert _contract_id("Покажи договор 88", previous) == "b"


def test_request_for_details_does_not_force_document_creation():
    from bis.agents.base import requests_write

    assert not requests_write("Напиши реквизиты заказчика")
    assert not requests_write("Составь список договоров")
    assert requests_write("Подготовь письмо заказчику")


def test_openai_embeddings_work_without_pgvector(db, demo, monkeypatch):
    import httpx

    from bis.config import Settings
    from bis.models import knowledge
    from bis.rag import search
    from bis.rag.embeddings import OpenAICompatEmbedder
    from bis.rag.ingest import ingest_text

    vector = [1.0] + [0.0] * 1023

    def response(request):
        texts = json.loads(request.content)["input"]
        return httpx.Response(200, json={"data": [{"index": i, "embedding": vector} for i in range(len(texts))]})

    embedder = OpenAICompatEmbedder(Settings(embedding_url="http://embeddings.test/v1", embedding_dim=1024),
        client=httpx.Client(base_url="http://embeddings.test/v1", transport=httpx.MockTransport(response)))
    monkeypatch.setattr(knowledge, "VECTOR", False)
    monkeypatch.setattr(search, "VECTOR", False)
    src = ingest_text(db, embedder, org_id=demo.id, kind="org_document", title="Специальный документ",
                      text="Редкое содержимое для проверки смыслового поиска.")
    chunk = db.scalar(select(KnowledgeChunk).where(KnowledgeChunk.source_id == src.id))
    assert chunk.embedding is not None
    hits = search.hybrid_search(db, embedder, "совершенно другая формулировка", org_id=demo.id,
                                include_global=False, source_ids=[src.id], k=1)
    assert hits and hits[0].source_id == str(src.id)


def test_repeated_deflection_returns_existing_evidence_instead_of_requesting_upload(client, auth):
    llm = ScriptedProvider(["Предоставьте номер договора.", "Не могу предоставить данные, пока вы не пришлёте договор."])
    set_gateway(LLMGateway(entries={"local": ProviderEntry(llm, "confidential")}))
    response = client.post("/api/chat", headers=auth, json={"message": "Какие сроки оплаты в договоре?"})
    assert response.status_code == 200
    answer = response.json()["text"]
    assert "выдержки" in answer and "ГП-01/2026" in answer
    assert "Предоставьте" not in answer and "не пришлёте договор" not in answer


def test_clarification_includes_concrete_archived_contracts(client, auth, db, demo):
    ctx = context(db, demo)
    archive_doc(ctx, "Договор № ДОП-88. Предмет: ремонт кровли.", number="ДОП-88")
    db.commit()
    llm = ScriptedProvider(["О каком договоре идёт речь?"])
    set_gateway(LLMGateway(entries={"local": ProviderEntry(llm, "confidential")}))
    response = client.post("/api/chat", headers=auth, json={"message": "Какой срок оплаты по договору?"})
    assert response.status_code == 200
    answer = response.json()["text"]
    assert "ДОП-88" in answer and "ГП-01/2026" in answer and "Какой из них" in answer


def test_closing_does_not_choose_arbitrary_contract_when_two_have_unclosed_work(db, demo):
    from bis.agents.tools_works import closing_check_tool
    from bis.models import EstimateItem, WorkVolume

    ctx = context(db, demo)
    contracts = db.scalars(select(Contract).where(Contract.organization_id == demo.id)).all()
    for i, contract in enumerate(contracts):
        item = EstimateItem(organization_id=demo.id, contract_id=contract.id,
                            name=f"Работа {i}", unit="м3", quantity=10)
        db.add(item)
        db.flush()
        from datetime import date

        from bis.models import ConstructionObject

        obj = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
        db.add(WorkVolume(organization_id=demo.id, object_id=obj.id, estimate_item_id=item.id,
                          quantity=1, date=date(2026, 5, 1), reported_by="test"))
    db.flush()
    raw, error = closing_check_tool.run(ctx, {})
    assert error and len(json.loads(raw)["candidates"]) == len(contracts)


def test_unread_file_is_reported_as_reading_problem_not_missing_contract(db, demo):
    from bis.agents.grounding import recovery_answer, remember
    from bis.agents.tools_control import get_document

    ctx = context(db, demo)
    doc = archive_doc(ctx, "", indexed=False)
    raw, error = get_document.run(ctx, {"document_id": str(doc.id)})
    assert not error
    remember(ctx, "get_document", {"document_id": str(doc.id)}, raw, error)
    answer = recovery_answer(ctx)
    assert "Договор № 77" in answer and "Извлечённого текста пока нет" in answer
    assert "повторно загружать" in answer
