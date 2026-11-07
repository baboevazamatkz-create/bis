from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.api.deps import agent_context, require
from bis.db import get_db
from bis.llm.gateway import get_gateway
from bis.llm.types import Message
from bis.models import ChatMessage, ChatSession, User
from bis.plugins.sdk import get_registry

router = APIRouter(tags=["chat"])
HISTORY_LIMIT = 20


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    session_id: uuid.UUID | None = None
    agent: str | None = None


@router.get("/agents")
def agents(user: User = Depends(require("knowledge:read"))):
    reg = get_registry()
    return [{"name": a.name, "title": a.title, "description": a.description, "tools": a.tools}
            for a in reg.agents.values()]


@router.get("/tools")
def tools(user: User = Depends(require("knowledge:read"))):
    """Инструменты платформы в формате MCP (tools/list)."""
    return {"tools": [t.mcp_definition() | {"scopes": list(t.scopes)} for t in get_registry().tools.all()]}


@router.post("/chat")
def chat(body: ChatIn, db: Session = Depends(get_db), user: User = Depends(require("knowledge:read"))):
    return _answer(db, user, body)


def _answer(db: Session, user: User, body: ChatIn, progress=None, on_route=None) -> dict:
    reg = get_registry()
    if body.session_id:
        session = db.get(ChatSession, body.session_id)
        if session is None or session.user_id != user.id:
            raise HTTPException(404, "Диалог не найден")
    else:
        session = ChatSession(organization_id=user.organization_id, user_id=user.id, title=body.message[:80])
        db.add(session)
        db.flush()
    rows = db.scalars(select(ChatMessage).where(ChatMessage.session_id == session.id)
                      .order_by(ChatMessage.created_at.desc()).limit(HISTORY_LIMIT)).all()
    history = [Message(role=m.role, content=m.content) for m in reversed(rows)]

    try:
        agent_name, routed_by = reg.supervisor().route(body.message, get_gateway(), body.agent)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    agent = reg.agents[agent_name]
    if on_route:
        on_route(agent_name, agent.title)
    ctx = agent_context(db, user)
    ctx.progress = progress
    result = agent.run(ctx, reg.tools, history, body.message)

    now = datetime.now(UTC)
    db.add(ChatMessage(session_id=session.id, role="user", content=body.message, created_at=now))
    db.add(ChatMessage(
        session_id=session.id, role="assistant", content=result.text, agent=agent_name,
        created_at=now + timedelta(microseconds=1),
        meta={"citations": result.citations, "documents": result.documents, "trace": result.trace,
              "provider": result.provider, "model": result.model, "routed_by": routed_by},
    ))
    db.commit()
    return {
        "session_id": session.id,
        "agent": agent_name,
        "agent_title": agent.title,
        "routed_by": routed_by,
        "text": result.text,
        "citations": result.citations,
        "documents": result.documents,
        "trace": result.trace,
        "provider": result.provider,
        "model": result.model,
    }


@router.post("/chat/stream")
def chat_stream(body: ChatIn, user: User = Depends(require("knowledge:read"))):
    """Как /chat, но шаги работы ИИ приходят сразу (text/event-stream): маршрут, поиск, чтение документов, ответ."""
    import json
    import queue
    import threading

    from fastapi.responses import StreamingResponse

    from bis.db import SessionLocal

    events: queue.Queue = queue.Queue()
    user_id = user.id

    def work() -> None:
        with SessionLocal() as db:
            try:
                me = db.get(User, user_id)
                events.put({"type": "step", "label": "Разбираю вопрос"})
                out = _answer(db, me, body, progress=lambda e: events.put({"type": "step", "label": e["label"]}),
                              on_route=lambda name, title: events.put({"type": "route", "agent": name, "title": title}))
                events.put({"type": "done", "reply": out})
            except HTTPException as e:
                events.put({"type": "error", "detail": e.detail})
            except Exception as e:  # noqa: BLE001 — показать пользователю, а не оборвать поток
                events.put({"type": "error", "detail": f"Не удалось ответить: {e}"})

    threading.Thread(target=work, daemon=True, name="bis-chat").start()

    def stream():
        while True:
            try:
                e = events.get(timeout=15)
            except queue.Empty:
                yield ": ping\n\n"  # соединение живо, ИИ ещё работает
                continue
            yield "data: " + json.dumps(e, ensure_ascii=False, default=str) + "\n\n"
            if e["type"] in ("done", "error"):
                return

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Content-Encoding": "identity"})


@router.get("/chat/sessions")
def sessions(db: Session = Depends(get_db), user: User = Depends(require("knowledge:read"))):
    rows = db.scalars(select(ChatSession).where(ChatSession.user_id == user.id)
                      .order_by(ChatSession.created_at.desc()).limit(100))
    return [{"id": s.id, "title": s.title, "created_at": s.created_at} for s in rows]


@router.get("/chat/sessions/{session_id}")
def session_messages(session_id: uuid.UUID, db: Session = Depends(get_db),
                     user: User = Depends(require("knowledge:read"))):
    session = db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(404, "Диалог не найден")
    rows = db.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at))
    return [{"role": m.role, "content": m.content, "agent": m.agent, "meta": m.meta, "created_at": m.created_at}
            for m in rows]
