"""Чат потоком: шаги работы ИИ приходят по мере выполнения, в конце — ответ, как у /chat."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest


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


def test_stream_preserves_active_project_header(client, auth, monkeypatch):
    from bis.api import chat

    projects = client.get("/api/objects", headers=auth).json()
    project_id = projects[0]["id"]

    def answer(db, user, body, **kwargs):
        return {"project": str(body.object_id)}

    monkeypatch.setattr(chat, "_answer", answer)
    response = client.post("/api/chat/stream", headers={**auth, "X-BIS-Project": project_id},
                           json={"message": "Подготовь АООК по имеющимся актам", "workspace": "executive"})
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == {"type": "done", "reply": {"project": project_id}}


def test_stream_rejects_conflicting_projects_before_background_work(client, auth):
    projects = client.get("/api/objects", headers=auth).json()
    first = projects[0]["id"]
    other = client.post("/api/objects", headers=auth, json={"name": "Другой участок"}).json()["id"]
    response = client.post("/api/chat/stream", headers={**auth, "X-BIS-Project": first},
                           json={"message": "Подготовь акт", "object_id": other})
    assert response.status_code == 409


def _collect(response):
    async def consume():
        return [json.loads(chunk[6:].strip()) async for chunk in response.body_iterator]

    return asyncio.run(consume())


def test_background_session_setup_errors_are_returned_to_browser(monkeypatch):
    from bis import db
    from bis.api import chat

    def unavailable():
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(db, "SessionLocal", unavailable)
    response = chat.chat_stream(chat.ChatIn(message="Найди документ"),
                                db=SimpleNamespace(info={}), user=SimpleNamespace(id="fixture"))
    events = _collect(response)
    assert events[-1]["type"] == "error" and "database unavailable" in events[-1]["detail"]


def test_hung_background_work_has_a_deadline_and_cancellation(monkeypatch):
    from bis import db
    from bis.api import chat
    from bis.config import get_settings

    stopped = threading.Event()
    me = SimpleNamespace(id="fixture", is_active=True)

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args):
            return me

    def slow_answer(*args, is_cancelled=None, **kwargs):
        while not is_cancelled():
            stopped.wait(0.005)
        stopped.set()
        raise TimeoutError("cancelled")

    monkeypatch.setattr(db, "SessionLocal", Session)
    monkeypatch.setattr(chat, "_answer", slow_answer)
    monkeypatch.setattr(chat, "STREAM_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(get_settings(), "chat_timeout_seconds", 0.05)
    events = _collect(chat.chat_stream(chat.ChatIn(message="Подготовь акт"),
                                      db=SimpleNamespace(info={}), user=me))
    assert events[-1]["type"] == "error" and "время ожидания" in events[-1]["detail"]
    assert stopped.wait(1)


def test_cancelled_answer_cannot_commit_its_session(db, demo, monkeypatch):
    from sqlalchemy import func, select

    from bis.agents.base import AgentResult
    from bis.api import chat
    from bis.models import ChatSession, User
    from bis.plugins.sdk import get_registry

    cancelled = threading.Event()

    def answer(*args):
        cancelled.set()
        return AgentResult(text="Результат", agent="assistant")

    user = db.scalar(select(User).where(User.organization_id == demo.id))
    monkeypatch.setattr(get_registry().agents["assistant"], "run", answer)
    with pytest.raises(TimeoutError):
        chat._answer(db, user, chat.ChatIn(message="Контроль отмены", agent="assistant"),
                     is_cancelled=cancelled.is_set)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.title == "Контроль отмены")) == 0
