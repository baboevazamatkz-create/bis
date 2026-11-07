"""Загрузка архива организации: тип документа, реестр, база знаний, договоры, дубликаты."""

from __future__ import annotations

import io
from pathlib import Path

import ezdxf
from docx import Document as Docx


def _docx(lines: list[str]) -> bytes:
    d = Docx()
    for line in lines:
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _dxf() -> bytes:
    doc = ezdxf.new()
    doc.modelspace().add_text("Исполнительная схема колонн 2 этажа")
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode()


def _upload(client, auth, name, data, **form):
    r = client.post("/api/archive/import", headers=auth, files={"file": (name, data)}, data=form)
    assert r.status_code == 200, r.text
    return r.json()


def test_archive_import_mixed_folder(client, auth, db):
    import bis.demo as demo_pkg

    obj = client.get("/api/objects", headers=auth).json()[0]
    aosr = _docx(["АКТ освидетельствования скрытых работ № 12", "Объект: ЖК-01", "Армирование фундаментной плиты Пм1"])
    r1 = _upload(client, auth, "АОСР_12.docx", aosr)
    assert r1["type"] == "aosr" and r1["kind"] == "aosr" and r1["knowledge"] and r1["object_id"] == obj["id"]
    contract = (Path(demo_pkg.__file__).parent / "demo_contract.txt").read_bytes()
    r2 = _upload(client, auth, "Договор_ГП.txt", contract, folder="ЖК-01/Договоры")
    assert r2["type"] == "contract" and r2.get("obligations", 0) > 0
    letter = "АО «Заказчик-Девелопмент»\nИсх. № 101/ТН от 01.03.2026\nО графике работ\nПросим представить график.".encode()
    r3 = _upload(client, auth, "Письмо_101.txt", letter)
    assert r3["type"] == "letter" and r3["kind"] == "incoming_letter"
    r4 = _upload(client, auth, "ИГС_колонны.dxf", _dxf())
    assert r4["type"] == "scheme" and r4["text"] == "dxf"
    r5 = _upload(client, auth, "ИГС_колонны.dwg", b"AC1032 fake dwg")
    assert r5["type"] == "scheme" and r5["text"] == "none"
    assert _upload(client, auth, "копия АОСР_12.docx", aosr)["status"] == "duplicate"
    assert _upload(client, auth, "~$АОСР_12.docx", b"x")["status"] == "skipped"
    # письмо из архива не создаёт поручений
    tasks = client.get("/api/tasks?limit=500", headers=auth).json()
    assert not [t for t in tasks if "график" in t["title"].lower()]
    # архив доступен умному поиску
    res = client.get("/api/knowledge/search", headers=auth, params={"q": "армирование плиты Пм1 акт", "scope": "organization"}).json()
    assert any("АОСР" in h["title"] for h in res["results"])


def test_archive_dates_and_single_object(client, db):
    from conftest import login

    from bis.demo import create_organization
    from bis.models import Document
    from bis.services.archive import repair_archive

    create_organization(db, name="ООО Пилот", inn="1435000001", admin_email="pilot@p.ru", admin_password="12345678",
                        admin_name="Админ")
    db.commit()
    auth = login(client, "pilot@p.ru", "12345678")
    letter = "АО «Заказчик»\nИсх. № 112 от 28.04.2026\nО предоставлении ИД\nПросим предоставить.".encode()
    r = _upload(client, auth, "Вх 112.txt", letter)
    assert r["date"] == "2026-04-28" and r["object_id"] is None  # объекта ещё нет
    aosr = "АКТ освидетельствования скрытых работ № 3\nДаты: начало работ 22.04.2026, окончание работ 25.04.2026".encode()
    assert _upload(client, auth, "АОСР 3.txt", aosr)["date"] == "2026-04-25"

    obj = client.post("/api/objects", headers=auth, json={"name": "Жилой дом по ул. Ленина, 10"}).json()
    db.expire_all()
    assert db.get(Document, r["document_id"]).object_id is not None  # архив отнесён к единственному объекту
    later = _upload(client, auth, "Исх 45.txt", "ООО Пилот\nИсх. № 45 от 18.03.2026\nО поставке".encode())
    assert later["object_id"] == obj["id"]

    doc = db.get(Document, later["document_id"])  # загружено старой версией: дата = день загрузки
    doc.date, doc.data = doc.created_at.date(), {k: v for k, v in doc.data.items() if k != "date_checked"}
    db.commit()
    assert repair_archive(db)["dates"] >= 1
    assert db.get(Document, later["document_id"]).date.isoformat() == "2026-03-18"


def test_find_documents_in_archive_and_letter_direction(client, db):
    from conftest import login
    from sqlalchemy import select

    from bis.agents.base import AgentContext
    from bis.agents.builtin_tools import FindDocumentsArgs, find_documents
    from bis.demo import create_organization
    from bis.models import User
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    org, _ = create_organization(db, name="ООО «Пилот»", inn="1435000002", admin_email="p2@p.ru", admin_password="12345678",
                                 admin_name="Админ")
    db.commit()
    auth = login(client, "p2@p.ru", "12345678")
    aosr = ("АКТ освидетельствования скрытых работ № 1\nРаботы: армирование фундаментной плиты Фм1\n"
            "Материалы: арматура А500С d12 (документ о качестве № 4512 от 02.04.2026)\nокончание работ 12.04.2026").encode()
    _upload(client, auth, "АОСР 1 армирование.txt", aosr)
    ours = _upload(client, auth, "Исх 45.txt", "ООО «Пилот»\nИсх. № 45 от 18.03.2026\nДиректору ООО «Метиз»\nО поставке".encode())
    theirs = _upload(client, auth, "Вх 112.txt", "АО «Заказчик»\nИсх. № 112 от 28.04.2026\nДиректору ООО «Пилот»\nОб ИД".encode())
    assert ours["kind"] == "letter" and theirs["kind"] == "incoming_letter"  # исходящее / входящее

    user = db.scalar(select(User).where(User.organization_id == org.id))
    ctx = AgentContext(db=db, org_id=org.id, user=user, gateway=None, embedder=get_embedder(), storage=get_storage(), agents={})
    res = find_documents.handler(ctx, FindDocumentsArgs(query="сертификат на арматуру фундаментной плиты"))
    assert res["documents"] and res["documents"][0]["title"].startswith("АОСР")
    assert all(d["n"] for d in res["documents"]) and "4512" in res["documents"][0]["excerpt"]
    assert ctx.documents and ctx.citations  # карточки документов и источники для ссылок [n]
    by_kind = find_documents.handler(ctx, FindDocumentsArgs(kind="incoming_letter"))
    assert [d["title"] for d in by_kind["documents"]] == ["Вх 112"]


def test_search_survives_embedding_outage(db, demo):
    from bis.rag.search import hybrid_search

    class Down:
        semantic = True

        def embed_query(self, q):
            raise RuntimeError("сервис векторов недоступен")

    hits = hybrid_search(db, Down(), "армирование фундаментной плиты", org_id=demo.id, k=5)
    assert hits  # нашлось по словам
