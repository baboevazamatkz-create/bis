"""Анализ проектной документации: разделы, комплектность, спецификации, сверка со сметой, замечания."""

from __future__ import annotations

from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
from bis.llm.providers.offline import ScriptedProvider
from bis.llm.types import Message, ToolCall
from bis.services.project_docs import detect_section, heuristic_spec

SPEC = """ЖК-01-КЖ1. Фундаментная плита Пм1
Спецификация
Поз. | Наименование | Ед. | Кол.
1 | Бетонирование фундаментной плиты Пм1, бетон B25 W8 | м3 | 1530
2 | Армирование фундаментной плиты Пм1 | т | 84,2
3 | Закладные детали МН-1 | шт | 48
Итого 1662"""


def test_detect_sections():
    assert detect_section("ЖК-01-ИОС4.1 Отопление.pdf")["code"] == "ИОС4"
    assert detect_section("ЖК-01-КЖ1 Фундаментная плита.pdf") == {"stage": "РД", "code": "КЖ1", "title": None}
    assert detect_section("Раздел 9", "Мероприятия по обеспечению пожарной безопасности")["code"] == "ПБ"
    assert detect_section("Проект организации работ по сносу здания")["code"] == "ПОД"
    assert detect_section("Ведомость.xlsx")["code"] is None


def test_heuristic_spec():
    spec = heuristic_spec(SPEC)
    assert [(i.position, i.unit, i.quantity) for i in spec.items] == [("1", "м3", 1530.0), ("2", "т", 84.2), ("3", "шт", 48.0)]


def _upload(client, auth, name, text, object_id):
    r = client.post("/api/documents/upload", headers=auth, data={"kind": "project_doc", "object_id": object_id},
                    files={"file": (name, text.encode(), "text/plain")})
    assert r.status_code == 201, r.text
    return r.json()


def test_completeness_and_compare(client, auth):
    obj = client.get("/api/objects", headers=auth).json()[0]["id"]
    for name in ["ЖК-01-ПЗ.txt", "ЖК-01-АР.txt", "ЖК-01-КР.txt", "ЖК-01-ИОС1.txt", "Без шифра.txt"]:
        _upload(client, auth, name, "Текст раздела", obj)
    kzh = _upload(client, auth, "ЖК-01-КЖ1.txt", SPEC, obj)
    assert kzh["data"]["pd_section"]["code"] == "КЖ1"
    rep = client.get(f"/api/objects/{obj}/pd-completeness", headers=auth).json()
    present = {s["code"] for s in rep["sections"] if s["present"]}
    assert present == {"ПЗ", "АР", "КР", "ИОС1"}
    assert any(m.startswith("ПОС") for m in rep["missing_required"]) and not any(m.startswith("ИОС6") for m in rep["missing_required"])
    assert "КЖ1" in rep["rd_sets"] and rep["unrecognized"] == ["Без шифра.txt"]

    contract = next(c for c in client.get("/api/contracts", headers=auth).json() if c["number"] == "ГП-01/2026")
    cmp = client.post(f"/api/documents/{kzh['id']}/compare-estimate", headers=auth, json={"contract_id": contract["id"]}).json()
    by = {r["code"]: r for r in cmp["rows"]}
    assert by["1.3"]["status"] == "mismatch" and by["1.3"]["difference"] == 80  # 1530 по проекту против 1450 в смете
    assert by["1.2"]["status"] == "ok"
    assert [m["name"] for m in cmp["not_in_estimate"]] == ["Закладные детали МН-1"]


def test_project_agent_remarks(client, auth):
    obj = client.get("/api/objects", headers=auth).json()[0]["id"]
    llm = ScriptedProvider([
        Message("assistant", tool_calls=[ToolCall("1", "pd_completeness", {"object_id": obj})]),
        Message("assistant", tool_calls=[ToolCall("2", "create_remarks_sheet", {
            "object_id": obj, "project": "ЖК-01 Проектная документация",
            "remarks": [{"section": "ПОС", "text": "Раздел ПОС не представлен", "norm": "ПП РФ № 87", "severity": "критическое"}]})]),
        "Ведомость замечаний сформирована: 1 замечание.",
    ])
    set_gateway(LLMGateway(entries={"local": ProviderEntry(llm, "confidential")}))
    data = client.post("/api/chat", headers=auth, json={"message": "Проверь комплектность проектной документации"}).json()
    assert data["agent"] == "project" and data["documents"][0]["kind"] == "remarks"
    from bis.documents.common import docx_text

    content = client.get(f"/api/documents/{data['documents'][0]['id']}/file", headers=auth).content
    text = docx_text(content)
    assert "ВЕДОМОСТЬ ЗАМЕЧАНИЙ" in text and "Раздел ПОС не представлен" in text
