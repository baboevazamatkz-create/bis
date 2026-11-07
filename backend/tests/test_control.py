"""Договоры, обязательства, неустойки, поручения, уведомления, фоновый контроль сроков."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from conftest import login
from pydantic import BaseModel

from bis.llm.extract import extract, find_dates, split_windows
from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
from bis.llm.providers.offline import OfflineProvider, ScriptedProvider
from bis.llm.types import Message, ToolCall
from bis.services.contracts import calc_penalty, heuristic_extract

CONTRACT = (Path(__file__).parents[1] / "bis" / "demo" / "demo_contract.txt").read_text(encoding="utf-8")


def test_heuristic_contract_extraction():
    r = heuristic_extract(CONTRACT, kind="general", our_role="contractor", our_inn="7701000001")
    assert r.number == "ГП-01/2026" and r.date == dt.date(2026, 2, 15)
    assert r.amount == 850_000_000 and r.vat_rate == 20 and r.counterparty_inn == "7702000003"
    by_clause = {o.clause: o for o in r.obligations}
    assert by_clause["2.2"].party == "counterparty" and by_clause["2.2"].kind == "payment"
    assert by_clause["2.2"].due_date == dt.date(2026, 3, 1)
    assert by_clause["3.1"].party == "we" and by_clause["3.1"].kind == "works"
    assert by_clause["3.1"].penalty.startswith("п. 5.1")
    assert by_clause["2.3"].due_text.startswith("в течение 15 рабочих дней")
    assert by_clause["4.2"].kind == "insurance"
    assert "5.2" not in by_clause  # условие о пене — не обязательство
    assert any("одностороннего" in x for x in r.risks) and any("0,1%" in x for x in r.risks)
    assert r.end_date == dt.date(2027, 12, 31)


def test_subcontract_parties():
    text = "4.1. Субподрядчик обязуется выполнить работы до 01.06.2026.\n4.2. Генподрядчик обязуется оплатить работы до 15.06.2026."
    r = heuristic_extract(text, kind="subcontract", our_role="customer")
    parties = {o.clause: o.party for o in r.obligations}
    assert parties == {"4.1": "counterparty", "4.2": "we"}


def test_penalty_calc():
    assert calc_penalty(1_000_000, 10, rate_percent_per_day=0.1).penalty == 10_000
    assert calc_penalty(1_000_000, 30, key_rate_percent=16, key_rate_fraction="1/300").penalty == 16_000
    capped = calc_penalty(1_000_000, 100, rate_percent_per_day=0.1, cap_percent=5)
    assert capped.penalty == 50_000 and "ограничено" in capped.formula
    with pytest.raises(ValueError):
        calc_penalty(100, 1)


class Extraction(BaseModel):
    number: str | None = None
    items: list[str] = []


def _gw(provider):
    return LLMGateway(entries={"p": ProviderEntry(provider, "confidential")})


def test_extract_retries_on_invalid_json_and_merges_windows():
    llm = ScriptedProvider(['{"number": 5}', '{"number": "A-1", "items": ["x"]}'])
    r = extract(_gw(llm), Extraction, "текст", instructions="Извлеки")
    assert r.data.number == "A-1" and len(llm.calls) == 2 and r.errors
    assert "JSON не прошёл проверку" in llm.calls[1]["messages"][-1].content

    llm = ScriptedProvider(['{"number": "A-1", "items": ["x", "y"]}', '{"number": null, "items": ["y", "z"]}'])
    r = extract(_gw(llm), Extraction, "абзац\n" * 30, instructions="Извлеки", window_chars=100)
    assert r.data.number == "A-1" and r.data.items == ["x", "y", "z"]


def test_extract_fallback_without_model():
    r = extract(_gw(OfflineProvider()), Extraction, "текст", instructions="x", fallback=lambda t: Extraction(number="F"))
    assert r.used_fallback and r.data.number == "F"


def test_dates_and_windows():
    dates = [d for _, d in find_dates("до 01.03.2026 и «15» марта 2026 г., 31.02.2026 — ошибка")]
    assert dates == [dt.date(2026, 3, 1), dt.date(2026, 3, 15)]
    windows = split_windows("строка\n" * 1000, 1000, overlap=100)
    assert len(windows) > 5 and all(len(w) <= 1000 for w in windows)


# --- API и фоновые задачи ---------------------------------------------------------------------


def _contract(client, auth, number="ГП-01/2026"):
    return next(c for c in client.get("/api/contracts", headers=auth).json() if c["number"] == number)


def test_demo_contract_analyzed_on_seed(client, auth):
    c = _contract(client, auth)
    obligations = client.get(f"/api/obligations?filter_field=contract_id&filter_value={c['id']}", headers=auth).json()
    assert len(obligations) >= 7
    ours = [o for o in obligations if o["party"] == "we"]
    assert all(o["responsible_id"] == c["responsible_id"] for o in ours)


def test_contract_import_from_file(client, auth):
    text = "ДОГОВОР ПОСТАВКИ № П-77\n«10» марта 2026 г.\n1. ПРЕДМЕТ\n1.1. Поставщик обязуется поставить арматуру А500С " \
           "до 20.03.2026.\n2. ОПЛАТА\n2.1. Покупатель обязуется оплатить товар в течение 5 дней после поставки.\n" \
           "Цена 1 200 000,00 руб., в т.ч. НДС 20 %."
    r = client.post("/api/contracts/import", headers=auth, data={"kind": "supply", "our_role": "customer"},
                    files={"file": ("supply.txt", text.encode(), "text/plain")})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["number"] == "П-77" and data["used_fallback"] is True and data["obligations_added"] == 2
    c = _contract(client, auth, "П-77")
    assert float(c["amount"]) == 1_200_000 and c["kind"] == "supply"
    res = client.get("/api/knowledge/search", headers=auth, params={"q": "поставка арматуры", "scope": "organization"})
    assert any("П-77" in x["title"] for x in res.json()["results"])


def test_deadlines_job_notifies_once(client, auth, db):
    from bis.db import SessionLocal
    from bis.jobs import run_job
    from bis.models import ContractObligation, Notification

    c = _contract(client, auth)
    run = run_job(SessionLocal, "deadlines", today=dt.date(2026, 7, 15))  # фундаменты (30.06) просрочены на 15 дней
    assert run.status == "ok"
    db.expire_all()
    overdue = db.query(ContractObligation).filter_by(status="overdue").all()
    assert any("фундамент" in o.description for o in overdue)
    n1 = db.query(Notification).count()
    assert n1 > 0
    assert db.query(Notification).filter_by(level="critical").count() >= 1  # просрочка ≥ 7 дней — администратору
    run_job(SessionLocal, "deadlines", today=dt.date(2026, 7, 15))
    assert db.query(Notification).count() == n1  # повторно не уведомляем
    notes = client.get("/api/notifications", headers=auth).json()
    assert notes and any("фундамент" in n["body"] for n in notes)
    client.post("/api/notifications/read-all", headers=auth)
    assert client.get("/api/control/summary", headers=auth).json()["notifications_unread"] == 0
    summary = client.get("/api/control/summary", headers=auth).json()
    assert summary["obligations_overdue"] >= 1
    _ = c


def test_tasks_crud_and_jobs_api(client, auth):
    persons = client.get("/api/persons", headers=auth).json()
    r = client.post("/api/tasks", headers=auth, json={"title": "Подготовить ИД за май", "due_date": "2026-01-01",
                                                      "assignee_person_id": persons[0]["id"], "priority": "high"})
    assert r.status_code == 201
    jobs = client.get("/api/jobs", headers=auth).json()
    assert any(j["name"] == "deadlines" for j in jobs)
    run = client.post("/api/jobs/deadlines/run", headers=auth).json()
    assert run["status"] == "ok" and run["result"]["overdue"] >= 1
    assert client.get("/api/control/summary", headers=auth).json()["tasks_overdue"] == 1
    pto = login(client, "pto@demo.bis", "demo12345")
    assert client.post("/api/jobs/deadlines/run", headers=pto).status_code == 403


def test_contracts_agent_penalty_and_claim(client, auth):
    c = _contract(client, auth)
    llm = ScriptedProvider([
        Message("assistant", tool_calls=[ToolCall("1", "get_contract_details", {"contract_id": c["id"]})]),
        Message("assistant", tool_calls=[ToolCall("2", "calc_penalty", {
            "base_amount": 85_000_000, "days": 20, "key_rate_percent": 16, "key_rate_fraction": "1/300", "cap_percent": 5})]),
        "Неустойка 906 666,67 руб. (п. 5.2 договора).",
    ])
    set_gateway(LLMGateway(entries={"local": ProviderEntry(llm, "confidential")}))
    data = client.post("/api/chat", headers=auth, json={"message": "Посчитай пени заказчику за просрочку аванса на 20 дней"}).json()
    assert data["agent"] == "contracts"
    penalty_result = [m for m in llm.calls[2]["messages"] if m.role == "tool"][-1].content
    assert "\"penalty\": 906666.67" in penalty_result
    details = [m for m in llm.calls[1]["messages"] if m.role == "tool"][0].content
    assert "ГП-01/2026" in details and "obligations" in details
