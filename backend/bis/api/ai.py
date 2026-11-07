"""ИИ-конвейер: приём сигналов с производства, лента действий ИИ, решения людей, специалисты и их самостоятельность."""

import datetime as dt
import uuid
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from bis.ai import conveyor as cv
from bis.ai.specialists import BY_NAME, SPECIALISTS, model_status, stats
from bis.api.deps import require
from bis.db import get_db
from bis.llm.gateway import get_gateway
from bis.models import AIAction, ConstructionObject, EstimateItem, IntakeEvent, Material, Organization, User
from bis.rag.embeddings import get_embedder
from bis.services.audit import audit
from bis.services.storage import get_storage

router = APIRouter(tags=["ai"])


def env_for(db: Session, user: User | None, org_id: uuid.UUID | None = None) -> cv.Env:
    return cv.Env(db=db, storage=get_storage(), gateway=get_gateway(), embedder=get_embedder(),
                  org_id=org_id or user.organization_id, user=user)


def action_out(a: AIAction) -> dict[str, Any]:
    spec = BY_NAME.get(a.specialist)
    return {
        "id": str(a.id), "event_id": str(a.event_id) if a.event_id else None, "specialist": a.specialist,
        "specialist_title": spec.title if spec else a.specialist, "kind": a.kind,
        "kind_title": cv.ACTION_TITLES.get(a.kind, a.kind), "title": a.title, "summary": a.summary, "payload": a.payload,
        "evidence": a.evidence, "confidence": a.confidence, "severity": a.severity, "status": a.status, "auto": a.auto,
        "corrected": a.corrected, "result": a.result, "object_id": str(a.object_id) if a.object_id else None,
        "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else None, "reason": a.reason,
        "created_at": a.created_at.isoformat() if a.created_at else None, "editable": list(cv.EDITABLE.get(a.kind, ())),
    }


def event_out(db: Session, e: IntakeEvent, actions: list[AIAction] | None = None) -> dict[str, Any]:
    if actions is None:
        actions = db.scalars(select(AIAction).where(AIAction.event_id == e.id).order_by(AIAction.created_at)).all()
    obj = db.get(ConstructionObject, e.object_id) if e.object_id else None
    r = e.result or {}
    return {
        "id": str(e.id), "created_at": e.created_at.isoformat() if e.created_at else None, "source": e.source,
        "kind": e.kind, "text": e.text, "file_name": e.file_name, "mime": e.mime,
        "file_url": f"/api/intake/{e.id}/file" if e.file_key else None,
        "object_id": str(e.object_id) if e.object_id else None, "object_name": obj.name if obj else None,
        "sender": e.sender, "status": e.status, "detected_type": e.detected_type, "dispatcher": r.get("dispatcher"),
        "transcript": r.get("transcript"), "excerpt": r.get("excerpt"), "reply": r.get("reply"),
        "needs_ocr": r.get("needs_ocr", False), "vision": r.get("vision"), "error": e.error,
        "document_id": str(e.document_id) if e.document_id else None, "actions": [action_out(a) for a in actions],
    }


def _owned(db: Session, user: User, model, item_id: uuid.UUID, what: str):
    row = db.get(model, item_id)
    if row is None or row.organization_id != user.organization_id:
        raise HTTPException(404, f"{what} не найден(о)")
    return row


@router.post("/intake")
async def intake(file: UploadFile | None = File(None), text: str | None = Form(None), object_id: uuid.UUID | None = Form(None),
                 db: Session = Depends(get_db), user: User = Depends(require("documents:write"))):
    """Принять сигнал с производства: фото, голосовое, документ или текст. ИИ разберёт и выполнит/предложит действия."""
    if file is None and not (text or "").strip():
        raise HTTPException(400, "Пришлите файл, фото, голосовое или текст")
    if object_id:
        _owned(db, user, ConstructionObject, object_id, "Объект")
    env = env_for(db, user)
    data = await file.read() if file is not None else None
    if data is not None and len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "Файл больше 50 МБ")
    event = cv.ingest(env, source="upload", text=text, data=data, file_name=file.filename if file else None,
                      mime=file.content_type if file else None, object_id=object_id)
    actions = cv.process(env, event)
    db.commit()
    return event_out(db, event, actions or None)


@router.get("/intake/{event_id}/file")
def intake_file(event_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    e = _owned(db, user, IntakeEvent, event_id, "Сообщение")
    if not e.file_key:
        raise HTTPException(404, "В сообщении нет файла")
    disposition = "inline" if (e.mime or "").startswith(("image/", "audio/")) else "attachment"
    return Response(content=get_storage().get(e.file_key), media_type=e.mime or "application/octet-stream",
                    headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(e.file_name or 'file')}"})


@router.get("/ai/feed")
def feed(object_id: uuid.UUID | None = None, only: Literal["all", "pending", "issues"] = "all",
         specialist: str | None = None, limit: int = 30, before: str | None = None,
         db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    """Лента: что пришло с объектов и что с этим сделал ИИ. pending — ждёт решения, issues — замечания."""
    stmt = select(IntakeEvent).where(IntakeEvent.organization_id == user.organization_id)
    if object_id:
        stmt = stmt.where(IntakeEvent.object_id == object_id)
    has = select(AIAction.id).where(AIAction.event_id == IntakeEvent.id)
    if only == "pending":
        stmt = stmt.where(exists(has.where(AIAction.status.in_(["proposed", "failed"]))))
    elif only == "issues":
        stmt = stmt.where(exists(has.where(AIAction.kind == "flag_issue", AIAction.severity != "info")))
    if specialist:
        stmt = stmt.where(exists(has.where(AIAction.specialist == specialist)))
    if before:
        try:
            stmt = stmt.where(IntakeEvent.created_at < dt.datetime.fromisoformat(before))
        except ValueError as e:
            raise HTTPException(400, "before — дата-время в формате ISO") from e
    events = db.scalars(stmt.order_by(IntakeEvent.created_at.desc()).limit(min(limit, 100))).all()
    by_event: dict[uuid.UUID, list[AIAction]] = {}
    if events:
        for a in db.scalars(select(AIAction).where(AIAction.event_id.in_([e.id for e in events])).order_by(AIAction.created_at)):
            by_event.setdefault(a.event_id, []).append(a)
    pending = db.scalar(select(func.count()).select_from(AIAction).where(
        AIAction.organization_id == user.organization_id, AIAction.status.in_(["proposed", "failed"]))) or 0
    return {"events": [event_out(db, e, by_event.get(e.id, [])) for e in events], "pending": pending}


@router.get("/ai/events/{event_id}")
def get_event(event_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    return event_out(db, _owned(db, user, IntakeEvent, event_id, "Сообщение"))


class ReviewIn(BaseModel):
    approve: bool
    reason: str | None = Field(None, description="Почему отклонено — ИИ учтёт в следующий раз")
    changes: dict[str, Any] | None = Field(None, description="Исправления перед утверждением: материал, позиция сметы, объём…")


@router.post("/ai/actions/{action_id}/review")
def review(action_id: uuid.UUID, body: ReviewIn, db: Session = Depends(get_db),
           user: User = Depends(require("documents:write"))):
    a = _owned(db, user, AIAction, action_id, "Действие")
    for key, model in (("material_id", Material), ("estimate_item_id", EstimateItem), ("object_id", ConstructionObject)):
        if (body.changes or {}).get(key):
            try:
                ref = uuid.UUID(str(body.changes[key]))
            except ValueError as e:
                raise HTTPException(400, f"Некорректный {key}") from e
            _owned(db, user, model, ref, "Элемент справочника")
    env = env_for(db, user)
    try:
        cv.review(env, a, approve=body.approve, user=user, reason=body.reason, changes=body.changes)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    db.commit()
    return {"action": action_out(a), "created": [action_out(x) for x in env.created]}


class BatchIn(BaseModel):
    ids: list[uuid.UUID]
    approve: bool
    reason: str | None = None


@router.post("/ai/actions/review-batch")
def review_batch(body: BatchIn, db: Session = Depends(get_db), user: User = Depends(require("documents:write"))):
    """Утвердить или отклонить несколько действий разом (например, все позиции накладной)."""
    done, errors = 0, []
    for aid in body.ids[:200]:
        a = db.get(AIAction, aid)
        if a is None or a.organization_id != user.organization_id:
            errors.append({"id": str(aid), "error": "не найдено"})
            continue
        try:
            with db.begin_nested():
                cv.review(env_for(db, user), a, approve=body.approve, user=user, reason=body.reason)
            done += 1
        except ValueError as e:
            errors.append({"id": str(aid), "error": str(e)})
    db.commit()
    return {"done": done, "errors": errors}


@router.get("/ai/specialists")
def specialists(db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    """ИИ-специалисты: в чём сильны, на какой модели работают, сколько сделали и насколько точны."""
    gw = get_gateway()
    st = stats(db, user.organization_id)
    env = env_for(db, user)
    autonomy = env.autonomy()
    return {
        "specialists": [{"name": s.name, "title": s.title, "strength": s.strength, "capability": s.capability,
                         "agent": s.agent, **model_status(gw, s), "stats": st.get(s.name, {})} for s in SPECIALISTS],
        "autonomy": [{"kind": k, "title": cv.ACTION_TITLES.get(k, k), "mode": autonomy.get(k, "propose"),
                      "default": cv.DEFAULT_AUTONOMY.get(k, "propose")} for k in cv.ACTION_TITLES],
        "min_confidence": cv.AUTO_MIN_CONFIDENCE,
        "suggestions": cv.autonomy_suggestions(db, user.organization_id, autonomy),
    }


class AutonomyIn(BaseModel):
    modes: dict[str, Literal["auto", "propose"]]


@router.put("/ai/autonomy")
def set_autonomy(body: AutonomyIn, db: Session = Depends(get_db), user: User = Depends(require("settings:write"))):
    """Что ИИ выполняет сам, а что только предлагает."""
    unknown = set(body.modes) - set(cv.ACTION_TITLES)
    if unknown:
        raise HTTPException(400, f"Неизвестные действия: {', '.join(sorted(unknown))}")
    org = db.get(Organization, user.organization_id)
    org.settings = {**(org.settings or {}), "autonomy": {**((org.settings or {}).get("autonomy") or {}), **body.modes}}
    audit(db, org.id, actor_type="user", actor=str(user.id), action="ai.autonomy", entity_type="organization",
          entity_id=org.id, details=body.modes)
    db.commit()
    return {"autonomy": {**cv.DEFAULT_AUTONOMY, **org.settings["autonomy"]}}


class AssembleIn(BaseModel):
    estimate_item_id: uuid.UUID


@router.post("/ai/assemble-aosr")
def assemble(body: AssembleIn, db: Session = Depends(get_db), user: User = Depends(require("documents:write"))):
    """Собрать АОСР по скрытой работе из накопленных фактов (объёмы, материалы с сертификатами, схемы, нормы, РД)."""
    item = _owned(db, user, EstimateItem, body.estimate_item_id, "Позиция сметы")
    if item.object_id is None:
        raise HTTPException(400, "Позиция сметы не привязана к объекту")
    env = env_for(db, user)
    event = cv.ingest(env, source="user", text=f"Собрать АОСР: {item.name}", object_id=item.object_id)
    event.detected_type, event.status = "request", "done"
    event.result = {"dispatcher": {"type": "request", "title": "Запрос пользователя", "confidence": 1.0, "by": "user"}}
    a = cv.assemble_aosr(env, event, item, item.object_id)
    if a is None:
        db.rollback()
        raise HTTPException(400, "Нет объёмов без АОСР по этой работе или черновик уже готовится")
    cv._decide_all(env)
    event.result = {**event.result, "reply": cv.reply_text(event, env.created)}
    db.commit()
    return event_out(db, event)


@router.post("/ai/models/check")
def models_check(user: User = Depends(require("settings:write"))):
    """Проверить подключение моделей (администратор): отвечает ли каждый провайдер, умеет ли JSON."""
    from bis.llm.check import check_providers

    return {"providers": check_providers(get_gateway())}
