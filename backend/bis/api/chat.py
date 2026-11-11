from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

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
STREAM_HEARTBEAT_SECONDS = 15
log = logging.getLogger(__name__)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    session_id: uuid.UUID | None = None
    agent: str | None = None
    object_id: uuid.UUID | None = None
    workspace: Literal["platform", "executive"] = "platform"
    document_id: uuid.UUID | None = None


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


def _answer(db: Session, user: User, body: ChatIn, progress=None, on_route=None, is_cancelled=None) -> dict:
    from bis.services.library_access import bind_context

    current = db.info.get("source_context")
    if body.object_id and current and current.object_id and current.object_id != body.object_id:
        raise HTTPException(409, "Объект запроса отличается от активного проекта")
    selected_project = body.object_id or (current.object_id if current else None)
    if not selected_project and body.session_id:
        own_session = db.scalar(select(ChatSession).where(ChatSession.id == body.session_id,
            ChatSession.user_id == user.id, ChatSession.organization_id == user.organization_id)
            .execution_options(library_management=True))
        if own_session:
            selected_project = own_session.object_id
    if not selected_project:
        from bis.brain.orchestrator import question_plan
        from bis.models import ConstructionObject
        from bis.services.executive_pipeline import is_work_report

        if question_plan(body.message)['predicates'] or is_work_report(body.message):
            projects = db.scalars(select(ConstructionObject.id).where(ConstructionObject.organization_id == user.organization_id,
                ConstructionObject.status == 'active').limit(2)).all()
            if len(projects) == 1:
                selected_project = projects[0]
    selected_document = None
    if body.document_id:
        from bis.models import Document

        selected_document = db.scalar(select(Document).where(Document.id == body.document_id,
            Document.organization_id == user.organization_id).execution_options(library_management=True))
        if selected_document is None:
            raise HTTPException(404, 'Документ не найден')
        if selected_document.object_id:
            if selected_project and selected_project != selected_document.object_id:
                raise HTTPException(409, 'Документ относится к другому проекту')
            selected_project = selected_document.object_id
    scope = bind_context(db, user.organization_id, selected_project)
    reg = get_registry()
    if body.session_id:
        session = db.get(ChatSession, body.session_id)
        if session is None or session.user_id != user.id or (scope.object_id and session.object_id != scope.object_id):
            raise HTTPException(404, "Диалог не найден")
        first = db.scalar(select(ChatMessage).where(ChatMessage.session_id == session.id, ChatMessage.role == "assistant")
                          .order_by(ChatMessage.created_at).limit(1))
        if first and (first.meta or {}).get("workspace", "platform") != body.workspace:
            raise HTTPException(404, "Диалог относится к другому рабочему пространству")
    else:
        session = ChatSession(organization_id=user.organization_id, user_id=user.id, title=body.message[:80], object_id=scope.object_id)
        db.add(session)
        db.flush()
    rows = db.scalars(select(ChatMessage).where(ChatMessage.session_id == session.id)
                      .order_by(ChatMessage.created_at.desc()).limit(HISTORY_LIMIT)).all()
    history = [Message(role=m.role, content=m.content) for m in reversed(rows)]

    explicit = body.agent
    if body.workspace == "executive":
        from bis.modules.executive import AGENTS

        if explicit and explicit not in AGENTS:
            raise HTTPException(400, "Специалист недоступен в блоке исполнительной документации")
        explicit = explicit or "assistant"
    try:
        agent_name, routed_by = reg.supervisor().route(body.message, get_gateway(), explicit)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    agent = reg.agents[agent_name]
    if on_route:
        on_route(agent_name, agent.title)
    if progress:
        progress({"label": "Готовлю данные выбранного проекта"})
    ctx = agent_context(db, user)
    ctx.workspace = body.workspace
    ctx.is_cancelled = is_cancelled
    ctx.check_cancelled()
    previous = next((m for m in rows if m.role == "assistant"), None)
    if previous:
        state = (previous.meta or {}).get("context") or {}
        ctx.focus = {k: v for k, v in state.get("focus", {}).items()
                     if k in ("document", "contract", "object", "fact_location", "fact_work", "fact_predicate", "fact_category") and isinstance(v, str)}
    if scope.strict:
        if session.object_id != scope.object_id:
            ctx.focus = {}
        if scope.object_id:
            ctx.focus['object'] = str(scope.object_id)
        else:
            ctx.focus = {}
    if selected_document:
        from bis.services.library_access import assert_document

        assert_document(db, selected_document)
        ctx.focus['document'] = str(selected_document.id)
        ctx.selected_document = str(selected_document.id)
    ctx.progress = progress
    from bis.services.executive_pipeline import is_work_report

    if body.workspace == "executive" and agent_name in ("pto", "assistant") and is_work_report(body.message):
        import json

        from bis.agents.base import AgentResult
        from bis.agents.tools_id import prepare_executive_package

        ctx.conversation = [*history, Message(role="user", content=body.message)]
        raw, error = prepare_executive_package.run(ctx, {"text": body.message})
        payload = json.loads(raw)
        if error:
            text = payload.get("error", "Не удалось собрать комплект")
        else:
            text = "Черновик комплекта ИД подготовлен." if payload.get("documents") else "Сообщение сохранено; для сборки нужны уточнения."
            if payload.get("gaps"):
                text += "\n\n" + "\n".join("• " + g["message"] for g in payload["gaps"])
            text += "\n\nРезультат и приложения доступны в разделе «Сообщить о работе». Утверждение — после проверки инженером."
        ctx.trace.append({"tool": "prepare_executive_package", "arguments": {"text": body.message}, "error": error})
        result = AgentResult(text=text, agent="pto", documents=ctx.documents, trace=ctx.trace, provider="executive_pipeline", model="evidence-v1")
    else:
        result = agent.run(ctx, reg.tools, history, body.message)

    now = datetime.now(UTC)
    db.add(ChatMessage(session_id=session.id, role="user", content=body.message, created_at=now))
    db.add(ChatMessage(
        session_id=session.id, role="assistant", content=result.text, agent=agent_name,
        created_at=now + timedelta(microseconds=1),
        meta={"citations": result.citations, "documents": result.documents, "trace": result.trace,
              "provider": result.provider, "model": result.model, "routed_by": routed_by,
              "context": {"focus": ctx.focus}, "workspace": body.workspace},
    ))
    ctx.check_cancelled()
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
def chat_stream(body: ChatIn, db: Session = Depends(get_db), user: User = Depends(require("knowledge:read"))):
    """Как /chat, но шаги работы ИИ приходят сразу (text/event-stream): маршрут, поиск, чтение документов, ответ."""
    import json
    import queue
    import threading

    from fastapi.responses import StreamingResponse

    from bis.config import get_settings
    from bis.db import SessionLocal

    events: queue.Queue = queue.Queue()
    user_id = user.id
    scope = db.info.get("source_context")
    project_id = scope.object_id if scope else None
    if project_id and body.object_id and project_id != body.object_id:
        raise HTTPException(409, "Объект запроса отличается от активного проекта")
    if project_id and not body.object_id:
        body = body.model_copy(update={"object_id": project_id})
    cancelled = threading.Event()
    started = time.monotonic()
    deadline = started + get_settings().chat_timeout_seconds

    def is_cancelled():
        return cancelled.is_set() or time.monotonic() >= deadline

    events.put({"type": "step", "label": "Проверяю проект и доступ к документам"})

    def work() -> None:
        try:
            with SessionLocal() as db:
                me = db.get(User, user_id)
                if me is None or not me.is_active:
                    raise HTTPException(401, "Пользователь недоступен")
                if is_cancelled():
                    return
                events.put({"type": "step", "label": "Разбираю вопрос"})
                out = _answer(db, me, body, progress=lambda e: events.put({"type": "step", "label": e["label"]}),
                              on_route=lambda name, title: events.put({"type": "route", "agent": name, "title": title}),
                              is_cancelled=is_cancelled)
                events.put({"type": "done", "reply": out})
        except HTTPException as e:
            events.put({"type": "error", "detail": e.detail})
        except Exception as e:  # noqa: BLE001 — показать пользователю, а не оборвать поток
            log.exception("Ошибка фонового ответа в чате")
            events.put({"type": "error", "detail": f"Не удалось ответить: {e}"})

    threading.Thread(target=work, daemon=True, name="bis-chat").start()

    def stream():
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    cancelled.set()
                    e = {"type": "error", "detail": "Превышено время ожидания ответа. Проверьте подключение модели. "
                         "Если создавался документ, проверьте реестр перед повторным запросом."}
                else:
                    try:
                        e = events.get(timeout=min(STREAM_HEARTBEAT_SECONDS, remaining))
                    except queue.Empty:
                        if time.monotonic() >= deadline:
                            continue
                        e = {"type": "step", "label": f"Ожидаю завершения запроса: {int(time.monotonic() - started)} с"}
                yield "data: " + json.dumps(e, ensure_ascii=False, default=str) + "\n\n"
                if e["type"] in ("done", "error"):
                    return
        finally:
            cancelled.set()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Content-Encoding": "identity"})


@router.get("/chat/sessions")
def sessions(workspace: Literal["platform", "executive"] = "platform", db: Session = Depends(get_db),
             user: User = Depends(require("knowledge:read"))):
    executive = select(ChatMessage.session_id).where(ChatMessage.role == "assistant",
                                                    ChatMessage.meta["workspace"].astext == "executive")
    scope = ChatSession.id.in_(executive) if workspace == "executive" else ChatSession.id.notin_(executive)
    rows = db.scalars(select(ChatSession).where(ChatSession.user_id == user.id, scope)
                      .order_by(ChatSession.created_at.desc()).limit(100))
    return [{"id": s.id, "title": s.title, "created_at": s.created_at} for s in rows]


@router.get("/chat/sessions/{session_id}")
def session_messages(session_id: uuid.UUID, workspace: Literal["platform", "executive"] = "platform", db: Session = Depends(get_db),
                     user: User = Depends(require("knowledge:read"))):
    session = db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(404, "Диалог не найден")
    rows = db.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at))
    messages = list(rows)
    first = next((m for m in messages if m.role == "assistant"), None)
    if first and (first.meta or {}).get("workspace", "platform") != workspace:
        raise HTTPException(404, "Диалог относится к другому рабочему пространству")
    return [{"role": m.role, "content": m.content, "agent": m.agent, "meta": m.meta, "created_at": m.created_at}
            for m in messages]
