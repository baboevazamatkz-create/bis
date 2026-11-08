"""«Эрудит»: карточки документов, связи (в т.ч. когда документ появился позже), досчёт, инструмент карточки."""

from sqlalchemy import select


def _upload(client, auth, name, text):
    r = client.post("/api/archive/import", headers=auth, files={"file": (name, text.encode())})
    assert r.status_code == 200, r.text
    return r.json()


def _org(client, db, inn="1435000099"):
    from conftest import login

    from bis.demo import create_organization

    org, _ = create_organization(db, name="ООО «Пилот»", inn=inn, admin_email=f"{inn}@p.ru", admin_password="12345678",
                                 admin_name="Админ")
    db.commit()
    return org, login(client, f"{inn}@p.ru", "12345678")


AOSR = """АКТ ОСВИДЕТЕЛЬСТВОВАНИЯ СКРЫТЫХ РАБОТ № 1
Работы: армирование фундаментной плиты Фм1 в осях 1-6/А-Г, отм. -3.300
Проектная документация: шифр 17-2026-КЖ0, листы 3-7
Материалы: арматура А500С d12, d16 (документ о качестве № 4512 от 02.04.2026)
Исполнительная схема № ИС-1
Даты: начало работ 05.04.2026, окончание работ 12.04.2026. СП 70.13330.2012 п. 5.16"""


def test_card_links_and_late_document(client, db):
    from bis.models import Document, DocumentLink

    org, auth = _org(client, db)
    r = _upload(client, auth, "АОСР 1.txt", AOSR)
    assert r["card"].startswith("АОСР № 1 от 12.04.2026") and r["links"] == 2
    doc = db.get(Document, r["document_id"])
    card = doc.data["card"]
    assert card["rd_code"] == "17-2026-КЖ0" and card["axes"] == ["1-6/А-Г"] and card["levels"] == ["-3.300"]
    assert {"А500С", "d12", "d16"} <= set(card["grades"]) and card["work_end"] == "2026-04-12"
    links = db.scalars(select(DocumentLink).where(DocumentLink.from_document_id == doc.id)).all()
    assert {(lk.relation, lk.ref_number, lk.to_document_id) for lk in links} == {("quality", "4512", None), ("scheme", "ИС-1", None)}

    cert = _upload(client, auth, "Сертификат 4512.txt", "Документ о качестве № 4512 от 02.04.2026. Арматура А500С d12")
    db.expire_all()
    q = db.scalar(select(DocumentLink).where(DocumentLink.from_document_id == doc.id, DocumentLink.relation == "quality"))
    assert str(q.to_document_id) == cert["document_id"]  # сертификат загрузили позже — связь нашлась сама

    letter = _upload(client, auth, "Вх 112.txt", "АО «Заказчик»\nИсх. № 112 от 28.04.2026\nДиректору ООО «Пилот»\n"
                                                 "На ваш исх. № 45 от 18.03.2026\nПросим до 08.05.2026 предоставить ИД "
                                                 "по АОСР № 1.")
    ldoc = db.get(Document, letter["document_id"])
    assert ldoc.data["card"]["deadline"] == "2026-05-08"
    rels = {lk.relation: lk for lk in db.scalars(select(DocumentLink).where(DocumentLink.from_document_id == ldoc.id))}
    assert str(rels["act"].to_document_id) == r["document_id"] and rels["letter"].to_document_id is None


def test_card_tool_and_backfill(client, db):
    from bis.agents.base import AgentContext
    from bis.agents.builtin_tools import CardArgs, FindDocumentsArgs, document_card, find_documents
    from bis.brain.build import build_missing, coverage
    from bis.models import Document, User
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    org, auth = _org(client, db, "1435000098")
    r = _upload(client, auth, "АОСР 1.txt", AOSR)
    doc = db.get(Document, r["document_id"])
    doc.data = {k: v for k, v in doc.data.items() if k not in ("card", "card_text")}  # как будто загружено до «Эрудита»
    db.commit()
    assert coverage(db, org.id)["pending"] == 1
    res = build_missing(db, None, get_embedder(), org.id)
    db.commit()
    assert res["cards"] == 1 and coverage(db, org.id)["pending"] == 0

    user = db.scalar(select(User).where(User.organization_id == org.id))
    ctx = AgentContext(db=db, org_id=org.id, user=user, gateway=None, embedder=get_embedder(), storage=get_storage(), agents={})
    out = document_card.handler(ctx, CardArgs(document_id=str(doc.id)))
    assert out["card"]["number"] == "1" and [x["number"] for x in out["references"]] == ["4512", "ИС-1"]
    found = find_documents.handler(ctx, FindDocumentsArgs(query="А500С 1-6/А-Г"))
    assert found["documents"][0]["id"] == str(doc.id) and found["documents"][0]["card"].startswith("АОСР № 1")


def test_vision_reads_scheme_sheets(client, db, monkeypatch):
    """Схема в PDF: листы → модель зрения → штамп, оси, отклонения в карточке и описание листа в поиске."""
    import io
    import json

    from pypdf import PdfWriter

    from bis.brain.build import coverage, see_missing, vision_missing
    from bis.llm.gateway import LLMGateway, ProviderEntry
    from bis.llm.providers.offline import ScriptedProvider
    from bis.models import Document, KnowledgeSource
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    org, auth = _org(client, db, "1435000097")
    w = PdfWriter()
    w.add_blank_page(width=400, height=300)
    buf = io.BytesIO()
    w.write(buf)
    r = client.post("/api/archive/import", headers=auth, files={"file": ("ИС-1 армирование плиты.pdf", buf.getvalue(),
                                                                          "application/pdf")}).json()
    assert r["type"] == "scheme"
    assert [d.id for d in vision_missing(db, org.id)] == [__import__("uuid").UUID(r["document_id"])]
    assert see_missing(db, None, get_embedder(), get_storage(), org.id)["vision"] == "не подключено"

    sheet = {"sheet_type": "исполнительная схема", "description": "Исполнительная схема армирования плиты Фм1 в осях 1-6/А-Г",
             "title_block": {"code": "17-2026-КЖ0", "sheet": "1", "title": "Плита Фм1", "revision": "2"},
             "elements": ["плита Фм1"], "axes": ["1-6", "А-Г"], "levels": ["-3.300"],
             "deviations": [{"point": "1/А", "plan_mm": "+8", "height_mm": "-5"}]}
    vlm = ScriptedProvider([json.dumps(sheet, ensure_ascii=False)] * 3)
    gw = LLMGateway(entries={"vlm": ProviderEntry(vlm, capabilities=("text", "vision"))},
                    routes={"default": ["vlm"], "vision": ["vlm"]})
    res = see_missing(db, gw, get_embedder(), get_storage(), org.id)
    db.commit()
    assert res["vision"] == 1 and res["waiting"] == 0 and vlm.calls[0]["messages"][1].images
    doc = db.get(Document, r["document_id"])
    card = doc.data["card"]
    assert card["rd_code"] == "17-2026-КЖ0" and card["revision"] == "2" and "1-6" in card["axes"]
    assert card["vision"]["sheets"][0]["deviations"][0]["plan_mm"] == "+8"
    src = db.scalars(select(KnowledgeSource).where(KnowledgeSource.document_id == doc.id)).all()
    assert any((s.meta or {}).get("vision") for s in src)
    assert coverage(db, org.id)["vision"] == 1


def test_vision_forbidden_keeps_documents_waiting(client, db):
    """Модель зрения есть в настройках, но закрыта (403): документы ждут, зрение помечено недоступным на 30 минут."""
    import io

    from pypdf import PdfWriter

    from bis.brain import vision
    from bis.brain.build import see_missing, vision_missing
    from bis.llm.gateway import LLMGateway, ProviderEntry
    from bis.llm.types import LLMError
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    class Forbidden:
        name, model = "yandex-vision", "gemma"

        def complete(self, *a, **k):
            raise LLMError('yandex-vision: HTTP 403: {"error":{"message":"Forbidden"}}', retryable=False)

    org, auth = _org(client, db, "1435000096")
    w = PdfWriter()
    w.add_blank_page(width=400, height=300)
    buf = io.BytesIO()
    w.write(buf)
    client.post("/api/archive/import", headers=auth, files={"file": ("ИС-2.pdf", buf.getvalue(), "application/pdf")})
    gw = LLMGateway(entries={"v": ProviderEntry(Forbidden(), capabilities=("text", "vision"))},
                    routes={"default": ["v"], "vision": ["v"]})
    vision._DOWN.update(until=None, reason=None)
    res = see_missing(db, gw, get_embedder(), get_storage(), org.id)
    assert res["vision"] == 0 and res["waiting"] == 1 and not res["vision_status"]["available"]
    assert "403" in res["vision_status"]["reason"] and len(vision_missing(db, org.id)) == 1
    vision._DOWN.update(until=None, reason=None)


def test_correspondence_chain(client, db):
    """Запрос заказчика → наш ответ → обещанный протокол, которого в архиве нет; срок прошёл — в первую очередь."""
    import datetime as dt

    from bis.brain.chains import correspondence

    org, auth = _org(client, db, "1435000095")
    _upload(client, auth, "АОСР 1.txt", "АКТ освидетельствования скрытых работ № 1\nРаботы: армирование фундаментной плиты Фм1\n"
                                        "окончание работ 12.04.2026")
    _upload(client, auth, "Вх 112.txt", "АО «Заказчик»\nИсх. № 112 от 28.04.2026\nДиректору ООО «Пилот»\nО предоставлении ИД\n"
                                        "Просим до 08.05.2026 предоставить ИД по АОСР № 1.")
    _upload(client, auth, "Вх 200.txt", "АО «Заказчик»\nИсх. № 200 от 10.05.2026\nДиректору ООО «Пилот»\nО графике\n"
                                        "Просим в срок до 01.06.2026 предоставить график работ.")
    _upload(client, auth, "Исх 51.txt", "ООО «Пилот»\nИсх. № 51 от 05.05.2026\nГенеральному директору АО «Заказчик»\n"
                                        "На ваш исх. № 112 от 28.04.2026\nНаправляем АОСР № 1. Протокол испытаний бетона № 56 "
                                        "и исполнительные схемы будут направлены до 15.05.2026.")
    res = correspondence(db, org.id, today=dt.date(2026, 11, 8))
    by = {r["label"].split(" от ")[0]: r for r in res["requests"]}
    assert by["№ 112"]["status"] == "answered" and by["№ 112"]["replied_by"][0]["label"].startswith("№ 51")
    assert by["№ 200"]["status"] == "overdue" and by["№ 200"]["deadline"] == "2026-06-01"
    (p,) = res["promises"]
    assert p["deadline"] == "2026-05-15" and p["overdue"] and [m["number"] for m in p["missing"]] == ["56"]
    assert any("протокол" in x.lower() and "№ 56" in x and "15.05.2026" in x and "срок прошёл" in x for x in res["open"])
    assert not any("2026-" in x for x in res["open"])  # в тексте для пользователя — даты по-русски
    assert res["open"][0].startswith(("Запрос № 200", "В письме"))  # просроченное — первым
    assert not any("№ 112" in x for x in res["open"])  # на запрос ответили — в открытых его нет


def test_selfcheck_generates_grounded_questions_and_scores(client, db):
    """Самопроверка: вопросы по архиву с эталоном из текста документа; правильный ответ засчитан, неправильный — нет."""
    from bis.agents.base import AgentResult
    from bis.brain import selfcheck
    from bis.models import User
    from bis.plugins.sdk import get_registry

    org, auth = _org(client, db, "1435000094")
    r = _upload(client, auth, "АОСР 1.txt", AOSR)
    cases = selfcheck.generate(db, org.id, n=10, per_doc=10)
    whats = {c["what"] for c in cases}
    assert {"поиск документа", "шифр РД", "дата окончания работ"} <= whats
    rd = next(c for c in cases if c["what"] == "шифр РД")
    assert rd["expect"] == ["17-2026-КЖ0"] and "АОСР № 1 от 12.04.2026" in rd["question"]

    def fake_run(self, ctx, registry, history, text):  # «ассистент»: знает шифр, путает дату
        answer = "Шифр 17-2026-КЖ0, лист 3-7." if "шифр" in text else "Работы окончены 13.04.2026."
        return AgentResult(text=answer, agent=self.name, documents=[{"id": r["document_id"]}])

    reg = get_registry()
    orig = type(next(iter(reg.agents.values()))).run
    type(next(iter(reg.agents.values()))).run = fake_run
    try:
        user = db.scalar(__import__("sqlalchemy").select(User).where(User.organization_id == org.id))
        own = selfcheck.parse_questions("Какой шифр у АОСР 1? → 17-2026-КЖ0\n# комментарий\nГде паспорт на бетон?")
        assert own[0]["expect"] == ["17-2026-КЖ0"] and own[1]["expect"] == []
        rep = selfcheck.run(db, user, [rd, next(c for c in cases if c["what"] == "дата окончания работ"), *own])
    finally:
        type(next(iter(reg.agents.values()))).run = orig
    s = rep["summary"]
    assert s["graded"] == 3 and s["correct"] == 2 and s["accuracy"] == 67 and s["found"] == 2
    assert s["by_what"]["дата окончания работ"] == {"ok": 0, "total": 1}


def test_find_documents_forgives_model_mistakes(client, db):
    """Модель путает вид письма, пишет дату по-русски и передаёт номер вместо id — документ всё равно находится."""
    from bis.agents.base import AgentContext
    from bis.agents.builtin_tools import CardArgs, FindDocumentsArgs, document_card, find_documents
    from bis.models import User
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    org, auth = _org(client, db, "1435000093")
    _upload(client, auth, "Исх 45.txt", "ООО «Пилот»\nИсх. № 45 от 18.03.2026\nДиректору ООО «Метиз»\nО поставке арматуры до 01.04.2026")
    user = db.scalar(select(User).where(User.organization_id == org.id))
    ctx = AgentContext(db=db, org_id=org.id, user=user, gateway=None, embedder=get_embedder(), storage=get_storage(), agents={})
    args = FindDocumentsArgs.model_validate({"query": "Письмо № 45", "kind": "incoming_letter",
                                             "date_from": "18.03.2026", "date_to": "18.03.2026"})
    res = find_documents.handler(ctx, args)
    assert res["documents"] and res["documents"][0]["title"] == "Исх 45"
    wrong_date = find_documents.handler(ctx, FindDocumentsArgs(query="Письмо № 45", date_from="2026-01-01", date_to="2026-01-02"))
    assert wrong_date["documents"] and "note" in wrong_date
    assert document_card.handler(ctx, CardArgs(document_id="45"))["title"] == "Исх 45"


def test_truncated_scan_is_reread_fully(client, db, monkeypatch):
    """Скан на 25 страниц, раньше прочитанный до 20-й: «Эрудит» дочитывает его целиком и находит конец документа."""
    import io

    from pypdf import PdfWriter

    from bis import ocr
    from bis.brain.build import reread_missing, unread_scans
    from bis.models import Document, KnowledgeChunk, KnowledgeSource
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    org, auth = _org(client, db, "1435000092")
    w = PdfWriter()
    for _ in range(25):
        w.add_blank_page(width=300, height=400)
    buf = io.BytesIO()
    w.write(buf)
    pages = [f"Страница {i + 1}. Договор подряда № 77 — условия." for i in range(25)]
    pages[-1] = "13. Адреса и реквизиты сторон. Заказчик: АО «Заказчик», ИНН 7701234567. Подрядчик: ООО «Пилот»."
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "recognize_pages", lambda data, mime, client=None: pages[:20])  # прежнее ограничение
    r = client.post("/api/archive/import", headers=auth, files={"file": ("Договор 77.pdf", buf.getvalue(), "application/pdf")}).json()
    doc = db.get(Document, r["document_id"])
    text = lambda: " ".join(db.scalars(select(KnowledgeChunk.content).join(KnowledgeSource).where(  # noqa: E731
        KnowledgeSource.document_id == doc.id)).all())
    assert "7701234567" not in text() and unread_scans(db, org.id)

    monkeypatch.setattr(ocr, "recognize_pages", lambda data, mime, client=None: pages)
    res = reread_missing(db, None, get_embedder(), get_storage(), org.id)
    db.commit()
    db.expire_all()
    assert res["reread"] == 1 and res["reread_left"] == 0 and "7701234567" in text()
    assert db.scalar(select(__import__("sqlalchemy").func.max(KnowledgeChunk.page)).join(KnowledgeSource).where(
        KnowledgeSource.document_id == doc.id)) == 25
    assert db.get(Document, doc.id).data["pages_checked"] == 25
