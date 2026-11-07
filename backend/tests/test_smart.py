"""Умные механизмы: тезаурус, проверка ссылок, самопроверка, делегирование, оценка качества."""

from __future__ import annotations

from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
from bis.llm.providers.offline import ScriptedProvider
from bis.llm.types import Message, ToolCall
from bis.rag.thesaurus import expand


def test_thesaurus_expand():
    assert "исполнительная документация" in expand("Когда сдавать ИД?")
    assert "засыпка пазух" in expand("требования к обратной засыпке")
    assert "армирование" in expand("сертификаты на арматуру")
    assert expand("Что-то совсем другое") == []
    assert "Узел У-1" in expand("узел у1 готов", {"узел у1": ["Узел У-1"]})
    assert expand("видео") == []  # «ид» внутри слова не считается


def test_org_terms_improve_search(client, auth):
    # собственный термин компании: «плита ПМ» = фундаментная плита
    client.post("/api/memory", headers=auth, json={"kind": "term", "content": "кротовина = обратная засыпка пазух"})
    res = client.get("/api/knowledge/search", headers=auth, params={"q": "кротовина", "scope": "norms"}).json()
    assert res["results"] and "3.1" in res["results"][0]["citation"]


def _gw(*responses):
    llm = ScriptedProvider(list(responses))
    set_gateway(LLMGateway(entries={"local": ProviderEntry(llm, "confidential")}))
    return llm


def test_citation_check_and_verification(client, auth):
    llm = _gw(
        Message("assistant", tool_calls=[ToolCall("1", "search_knowledge", {"query": "засыпка пазух слои", "scope": "norms"})]),
        "Засыпку выполняют слоями не более 0,3 м [1], коэффициент уплотнения 0,98 [7].",
        '{"supported": false, "issues": ["Коэффициент 0,98 не подтверждается: в п. 3.1.2 — не менее 0,95"]}',
    )
    data = client.post("/api/chat", headers=auth, json={"message": "Какой толщиной слоями засыпать пазухи по СП?"}).json()
    assert data["agent"] == "norms"
    assert "Ссылки [7] не соответствуют" in data["text"]
    assert "⚠ Самопроверка: Коэффициент 0,98 не подтверждается" in data["text"]
    assert data["citations"][0]["used"] is True
    verify_call = llm.calls[2]
    assert verify_call["json_mode"] and "<фрагменты>" in verify_call["messages"][1].content


def test_sources_appended_when_answer_has_no_refs(client, auth):
    _gw(Message("assistant", tool_calls=[ToolCall("1", "search_knowledge", {"query": "опалубка", "scope": "norms"})]),
        "Опалубку снимают при 70 % прочности.", '{"supported": true, "issues": []}')
    data = client.post("/api/chat", headers=auth, json={"message": "Когда снимать опалубку по СП?"}).json()
    assert "Источники: [1] ДЕМО-СП" in data["text"]


def test_delegation_assistant_to_contracts(client, auth):
    contract = next(c for c in client.get("/api/contracts", headers=auth).json() if c["number"] == "ГП-01/2026")
    llm = _gw(
        # помощник делегирует договорному агенту
        Message("assistant", tool_calls=[ToolCall("1", "delegate_to_agent", {
            "agent": "contracts", "task": f"Рассчитай пени по договору {contract['id']}: 1 000 000 руб., 10 дней, 0,1% в день"})]),
        # договорной агент считает
        Message("assistant", tool_calls=[ToolCall("2", "calc_penalty", {"base_amount": 1000000, "days": 10,
                                                                        "rate_percent_per_day": 0.1})]),
        "Пени 10 000 руб.",
        '{"supported": true, "issues": []}',
        "Договорной агент рассчитал пени: 10 000 руб.",
    )
    data = client.post("/api/chat", headers=auth, json={"message": "Привет, нужна помощь с расчётом", "agent": "assistant"}).json()
    assert data["agent"] == "assistant"
    tools = [t["tool"] for t in data["trace"]]
    assert tools[:2] == ["calc_penalty", "delegate_to_agent"] or "calc_penalty" in tools
    delegate_result = [m for m in llm.calls[-1]["messages"] if m.role == "tool"][-1].content
    assert '"agent": "contracts"' in delegate_result and "10 000" in delegate_result
    docs = client.get("/api/audit", headers=auth).json()
    assert any(a["actor"] == "contracts" and a["action"] == "tool.calc_penalty" for a in docs)


def test_delegation_depth_limited(client, auth):
    from bis.agents.base import AgentContext
    from bis.agents.tools_control import DelegateArgs, delegate_to_agent

    ctx = AgentContext(db=None, org_id=None, user=None, gateway=None, embedder=None, storage=None, depth=1, agents={})
    try:
        delegate_to_agent.handler(ctx, DelegateArgs(agent="norms", task="x"))
        raise AssertionError("ожидалась ошибка")
    except ValueError as e:
        assert "Повторное делегирование" in str(e)


def test_eval_demo(client, auth):
    report = client.post("/api/evals/run", headers=auth).json()
    assert report["cases"] == 10
    assert report["hit@5"] >= 0.8, report["results"]
    assert report["routing_accuracy"] >= 0.8, [r for r in report["results"] if "agent" in r]
