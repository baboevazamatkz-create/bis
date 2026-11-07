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
