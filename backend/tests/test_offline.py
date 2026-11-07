"""Офлайн-режим (без модели): типовые запросы отвечаются по реальным данным через инструменты агентов."""

from __future__ import annotations

import datetime as dt

import pytest

from bis.llm.providers.offline import _period


def test_period_parsing():
    assert _period("перерасход за май 2026") == {"period_start": "2026-05-01", "period_end": "2026-05-31"}
    assert _period("КС-2 за февраль 2028")["period_end"] == "2028-02-29"
    today = dt.date.today()
    assert _period("за этот месяц")["period_start"] == f"{today.year}-{today.month:02d}-01"
    assert _period("материалы на объекте")["period_start"].endswith("-01")  # «ма» в слове не май


@pytest.mark.parametrize("question, agent, expected", [
    ("Что просрочено?", "control", "просрочено"),
    ("Что просрочено по договорам?", "contracts", "просрочено"),
    ("Покажи план-факт по корпусу 1", "works", "План-факт"),
    ("Какой перерасход материалов за май 2026?", "materials", "ПЕРЕРАСХОД"),
    ("Что можно закрыть в КС-2 за май 2026 по договору ГП-01/2026?", "closing", "Итого 5 880 000 руб."),
    ("Какие риски в договоре ГП-01/2026?", "contracts", "Риски:"),
])
def test_offline_playbooks(client, auth, question, agent, expected):
    data = client.post("/api/chat", headers=auth, json={"message": question}).json()
    assert data["agent"] == agent, data
    assert expected in data["text"], data["text"]
    assert not any(t["error"] for t in data["trace"]), data["trace"]
