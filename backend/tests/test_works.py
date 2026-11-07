"""Учёт работ и материалов: план-факт, М-29, списание, импорт сметы, КС-2/КС-3, агенты."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal as D

import openpyxl

from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
from bis.llm.providers.offline import ScriptedProvider
from bis.llm.types import Message, ToolCall
from bis.services.closing import parse_estimate_xlsx
from bis.services.works import M29Line, compute_m29, compute_plan_fact_row, similarity, write_off

MAY = {"period_start": "2026-05-01", "period_end": "2026-05-31"}


def test_plan_fact_row():
    start, end = dt.date(2026, 5, 1), dt.date(2026, 5, 31)
    # к 16.05 по плану половина (50 из 100), факт 20 → отставание, прогноз позже плана
    r = compute_plan_fact_row(task_id="t", name="Работа", unit="м3", start=start, end=end, planned=D(100),
                              volumes=[(dt.date(2026, 5, 2), D(10)), (dt.date(2026, 5, 10), D(10))], on_date=dt.date(2026, 5, 16))
    assert r.status == "behind" and r.planned_to_date == D("51.6129") and r.actual == D(20)
    assert r.forecast_end > end
    done = compute_plan_fact_row(task_id="t", name="Р", unit=None, start=start, end=end, planned=D(10),
                                 volumes=[(dt.date(2026, 5, 3), D(12))], on_date=dt.date(2026, 5, 4))
    assert done.status == "done" and done.forecast_end == dt.date(2026, 5, 3)
    late = compute_plan_fact_row(task_id="t", name="Р", unit=None, start=start, end=end, planned=D(10), volumes=[],
                                 on_date=dt.date(2026, 6, 5))
    assert late.status == "overdue" and late.forecast_end is None
    assert compute_plan_fact_row(task_id="t", name="Р", unit=None, start=start, end=end, planned=D(10), volumes=[],
                                 on_date=dt.date(2026, 4, 20)).status == "not_started"


def test_m29_and_writeoff():
    line = M29Line("1.3", "Бетонирование", D(600), "м3", D(1), D("1.5"), D(609))
    rows = compute_m29({"b25": ("Бетон B25", "м3", [line])}, {"b25": D(625), "x": D(3)}, {"x": ("Пена", "шт")})
    by = {r.material: r for r in rows}
    assert by["Бетон B25"].status == "overspend" and by["Бетон B25"].deviation == D(16)
    assert by["Пена"].status == "no_norm"
    data = {"rows": [{"material": "Бетон B25", "unit": "м3", "norm_total": D(609), "actual": D(625), "avg_price": D(7200)}]}
    act = write_off(data)
    assert act["rows"][0]["write_off"] == D("621.18") and act["rows"][0]["excess"] == D("3.82")
    assert act["total"] == D("4472496.00")


def test_similarity():
    assert similarity("залили бетон плиты Пм1", "Бетонирование фундаментной плиты Пм1, бетон B25") > \
        similarity("залили бетон плиты Пм1", "Обратная засыпка пазух котлована")


def _xlsx(rows) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


ESTIMATE = [
    ["ЛОКАЛЬНЫЙ СМЕТНЫЙ РАСЧЁТ № 02-01"],
    ["Объект: корпус 1"],
    [],
    ["№ п/п", "Шифр расценки", "Наименование работ", "Ед. изм.", "Кол-во", "Цена за ед., руб.", "Стоимость, руб."],
    [1, 2, 3, 4, 5, 6, 7],
    [None, None, "Раздел 2. Каркас"],
    [1, "ГЭСН 06-01-015-01", "Армирование стен", "т", 12.5, 98000, 1225000],
    [2, "ГЭСН 06-01-034-01", "Бетонирование стен", "м3", "150,5", None, "1 474 900"],
    [None, None, "Итого по разделу 2", None, None, None, 2699900],
]


def test_parse_estimate_xlsx():
    items, cols = parse_estimate_xlsx(_xlsx(ESTIMATE))
    assert [i["name"] for i in items] == ["Армирование стен", "Бетонирование стен"]
    assert items[0]["section"] == "Раздел 2. Каркас" and items[0]["code"] == "ГЭСН 06-01-015-01"
    assert items[0]["requires_aosr"] is True and items[1]["requires_aosr"] is False
    assert items[1]["quantity"] == D("150.5") and items[1]["unit_price"] == D("9800.00")


# --- API ----------------------------------------------------------------------------------------


def _ids(client, auth):
    contract = next(c for c in client.get("/api/contracts", headers=auth).json() if c["number"] == "ГП-01/2026")
    return contract["id"], contract["object_id"]


def test_estimate_import_api(client, auth):
    contract_id, _ = _ids(client, auth)
    r = client.post(f"/api/contracts/{contract_id}/estimate/import", headers=auth,
                    files={"file": ("smeta.xlsx", _xlsx(ESTIMATE), "application/octet-stream")})
    assert r.status_code == 200, r.text
    assert r.json() == {"imported": 2, "hidden_works": 1, "total": 2699900.0}


def test_plan_fact_and_balance_api(client, auth):
    _, object_id = _ids(client, auth)
    pf = client.get(f"/api/objects/{object_id}/plan-fact", headers=auth, params={"on_date": "2026-06-01"}).json()
    by = {r["name"][:20]: r for r in pf["rows"]}
    assert by["Устройство бетонной "]["status"] == "done"
    assert by["Бетонирование фундам"]["status"] == "on_track"
    bal = {b["material"]: b for b in client.get(f"/api/objects/{object_id}/materials/balance", headers=auth).json()}
    assert float(bal["Бетон B25 W8 F150"]["balance"]) == -5.0
    assert bal["Бетон B25 W8 F150"]["problem"]


def test_m29_api(client, auth):
    _, object_id = _ids(client, auth)
    data = client.post("/api/materials/m29/preview", headers=auth, json={"object_id": object_id, **MAY}).json()
    by = {r["material"]: r for r in data["rows"]}
    assert by["Бетон B25 W8 F150"]["norm_total"] == 609.0 and by["Бетон B25 W8 F150"]["status"] == "overspend"
    assert by["Бетон B7,5"]["status"] == "ok"
    assert any("Перерасход Бетон B25" in i["message"] for i in data["issues"])
    doc = client.post("/api/materials/m29", headers=auth, json={"object_id": object_id, **MAY}).json()
    assert doc["kind"] == "m29"
    content = client.get(f"/api/documents/{doc['id']}/file", headers=auth).content
    ws = openpyxl.load_workbook(io.BytesIO(content)).active
    text = " ".join(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)
    assert "Бетон B25 W8 F150" in text and "М-29" in text
    act = client.post("/api/materials/write-off", headers=auth, json={"object_id": object_id, **MAY}).json()
    assert act["kind"] == "writeoff" and any("Сверх нормы" in i["message"] for i in act["issues"])


def test_closing_flow(client, auth):
    contract_id, _ = _ids(client, auth)
    check = client.post(f"/api/contracts/{contract_id}/closing/check", headers=auth, json=MAY).json()
    assert [r["code"] for r in check["rows"]] == ["1.3"]  # бетонирование; подготовке нужен АОСР
    assert check["total"] == 600 * 9800 and check["vat"] == 600 * 9800 * 0.2
    messages = " ".join(i["message"] for i in check["issues"])
    assert "без утверждённого АОСР" in messages and "Не подтверждено ПТО" in messages

    r = client.post(f"/api/contracts/{contract_id}/closing/ks2", headers=auth, json=MAY)
    assert r.status_code == 201, r.text
    res = r.json()
    ks2, ks3 = res["ks2"], res["ks3"]
    assert ks2["kind"] == "ks2" and ks3["kind"] == "ks3" and ks2["number"] == "1"
    ws = openpyxl.load_workbook(io.BytesIO(client.get(f"/api/documents/{ks2['id']}/file", headers=auth).content)).active
    values = [c.value for row in ws.iter_rows() for c in row if c.value is not None]
    assert "АКТ О ПРИЁМКЕ ВЫПОЛНЕННЫХ РАБОТ" in values and 5880000 in values
    # повторно те же объёмы не закрываются
    again = client.post(f"/api/contracts/{contract_id}/closing/check", headers=auth, json=MAY).json()
    assert again["rows"] == []
    # утверждение КС-2 закрывает объёмы; следующий КС-3 учитывает его «с начала работ»
    assert client.post(f"/api/documents/{ks2['id']}/approve", headers=auth).status_code == 200
    vols = client.get("/api/volumes?filter_field=status&filter_value=closed", headers=auth).json()
    assert len(vols) == 2


def test_closing_with_aosr(client, auth):
    """Подготовка закрывается после привязки утверждённого АОСР к объёмам."""
    contract_id, object_id = _ids(client, auth)
    body = {"object_id": object_id, "works": "Устройство бетонной подготовки", "project_docs": "ЖК-01-КЖ0",
            "materials": ["Бетон B7,5, паспорт № 1"], "executive_docs": ["ИС № 1"], "work_start": "2026-05-04",
            "work_end": "2026-05-08", "norms": ["ДЕМО-СП 999.13330.2026, п. 2.2"], "next_works": "Армирование",
            "act_date": "2026-05-09"}
    aosr = client.post("/api/documents/aosr", headers=auth, json=body).json()
    client.post(f"/api/documents/{aosr['id']}/approve", headers=auth)
    items = client.get(f"/api/estimate-items?filter_field=contract_id&filter_value={contract_id}", headers=auth).json()
    prep = next(i for i in items if i["code"] == "1.1")
    vols = client.get(f"/api/volumes?filter_field=estimate_item_id&filter_value={prep['id']}", headers=auth).json()
    r = client.post("/api/volumes/confirm", headers=auth, json={"volume_ids": [v["id"] for v in vols], "aosr_document_id": aosr["id"]})
    assert r.status_code == 200
    check = client.post(f"/api/contracts/{contract_id}/closing/check", headers=auth, json=MAY).json()
    assert sorted(r["code"] for r in check["rows"]) == ["1.1", "1.3"]


def test_works_agent_reports_volume(client, auth):
    contract_id, _ = _ids(client, auth)
    items = client.get(f"/api/estimate-items?filter_field=contract_id&filter_value={contract_id}", headers=auth).json()
    concrete = next(i for i in items if i["code"] == "1.3")
    llm = ScriptedProvider([
        Message("assistant", tool_calls=[ToolCall("1", "find_estimate_items", {"query": "бетон плиты Пм1"})]),
        Message("assistant", tool_calls=[ToolCall("2", "report_volume", {"estimate_item_id": concrete["id"], "quantity": 250,
                                                                         "date": "2026-06-03", "location": "захватка 3"})]),
        "Записал 250 м3, накоплено 850 из 1450 м3 (58,6%).",
    ])
    set_gateway(LLMGateway(entries={"local": ProviderEntry(llm, "confidential")}))
    data = client.post("/api/chat", headers=auth, json={"message": "Залили 250 м3 бетона плиты Пм1, захватка 3"}).json()
    assert data["agent"] == "works"
    found = [m for m in llm.calls[1]["messages"] if m.role == "tool"][0].content
    assert concrete["id"] in found.split('"match"')[0]  # лучшая позиция — первой
    report = [m for m in llm.calls[2]["messages"] if m.role == "tool"][-1].content
    assert '"cumulative": "850' in report and '"percent": 58.6' in report


def test_plan_fact_job(db, demo):
    from bis.db import SessionLocal
    from bis.jobs import load_all_jobs, run_job
    from bis.models import Notification

    load_all_jobs()
    run = run_job(SessionLocal, "plan_fact", today=dt.date(2026, 6, 20))
    assert run.status == "ok"
    db.expire_all()
    titles = [n.title for n in db.query(Notification).all()]
    assert any("Армирование фундаментной плиты" in t for t in titles)  # 40 т из 85 при плане «всё к 10.06»
