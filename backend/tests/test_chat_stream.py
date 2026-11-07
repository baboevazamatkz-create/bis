"""Чат потоком: шаги работы ИИ приходят по мере выполнения, в конце — ответ, как у /chat."""

import json


def test_chat_stream_events(client, auth):
    events = []
    with client.stream("POST", "/api/chat/stream", headers=auth,
                       json={"message": "Какая допустимая толщина слоя обратной засыпки?"}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "step" and "route" in kinds and kinds[-1] == "done"
    reply = events[-1]["reply"]
    assert reply["text"] and reply["session_id"] and reply["agent"]
    history = client.get(f"/api/chat/sessions/{reply['session_id']}", headers=auth).json()
    assert [m["role"] for m in history] == ["user", "assistant"]


def test_step_labels():
    from bis.agents.base import step_label

    assert step_label("find_documents", {"query": "АОСР армирование"}) == "Ищу в документах: «АОСР армирование»"
    assert step_label("unknown_tool", {}) == "Работаю: unknown_tool"
