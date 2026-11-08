"""«Ось»: карточки документов, связи (в т.ч. когда документ появился позже), досчёт, инструмент карточки."""

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
    doc.data = {k: v for k, v in doc.data.items() if k not in ("card", "card_text")}  # как будто загружено до «Оси»
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
