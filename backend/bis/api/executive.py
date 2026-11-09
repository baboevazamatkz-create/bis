"""Persistent executive-document jobs, with background processing and explicit resolution of gaps."""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bis.api.deps import agent_context, require
from bis.db import get_db
from bis.models import Document, IntakeEvent, User
from bis.services import executive_pipeline as pipeline
from bis.services.audit import audit

router = APIRouter(prefix="/executive/runs", tags=["executive"])
log = logging.getLogger(__name__)


def owned(db, user, run_id, lock=False):
    stmt = select(IntakeEvent).where(IntakeEvent.id == run_id, IntakeEvent.organization_id == user.organization_id,
                                    IntakeEvent.result["pipeline"].astext == str(pipeline.VERSION))
    scope = db.info.get('source_context')
    if scope and scope.object_id:
        stmt = stmt.where(IntakeEvent.object_id == scope.object_id)
    if lock:
        stmt = stmt.with_for_update()
    event = db.scalar(stmt)
    if event is None:
        raise HTTPException(404, "Задание не найдено")
    return event


def execute(run_id, user_id):
    from bis.db import SessionLocal

    with SessionLocal() as db:
        event = db.scalar(select(IntakeEvent).where(IntakeEvent.id == run_id).with_for_update())
        user = db.get(User, user_id)
        if event is None or user is None or event.organization_id != user.organization_id or event.result.get("status") != "queued":
            return
        from bis.security import has_scope

        if not user.is_active or not has_scope(user.role, "documents:write"):
            event.result = {**event.result, "status": "error", "error": "У автора задания больше нет прав на подготовку документов"}
            db.commit()
            return
        token = str(uuid.uuid4())
        event.result = {**event.result, "status": "running", "attempt": token, "started_at": dt.datetime.now(dt.UTC).isoformat()}
        db.commit()
        try:
            from bis.services.library_access import bind_context

            bind_context(db, user.organization_id, event.object_id)
            ctx = agent_context(db, user)
            ctx.workspace = "executive"

            def checkpoint(_=None, commit=True):
                with db.no_autoflush:
                    current = db.scalar(select(IntakeEvent.result["attempt"].astext).where(IntakeEvent.id == run_id).with_for_update().execution_options(library_management=True))
                if current != token:
                    raise ValueError("Попытка обработки заменена более новой")
                if commit:
                    event.result = {**event.result, "heartbeat_at": dt.datetime.now(dt.UTC).isoformat()}
                    db.commit()

            pipeline.process_run(ctx, event, progress=checkpoint)
            checkpoint(commit=False)
            audit(db, user.organization_id, actor_type="agent", actor="executive", action="executive.prepare",
                  entity_type="intake_event", entity_id=event.id, details={"status": event.result["status"], "revision": event.result["revision"]})
            db.commit()
        except Exception as e:  # noqa: BLE001 — a failed model/file must not lose the user's report
            db.rollback()
            log.exception("Ошибка сборки ИД %s", run_id)
            event = db.get(IntakeEvent, run_id)
            if event.result.get("attempt") != token:
                return
            event.status = "error"
            event.result = {**event.result, "status": "error", "error": str(e)[:500] if isinstance(e, ValueError | LookupError)
                            else "Не удалось завершить обработку. Сообщение сохранено; можно повторить проверку."}
            db.commit()


@router.post("", status_code=202)
def create(body: pipeline.RunInput, tasks: BackgroundTasks, db: Session = Depends(get_db),
           user: User = Depends(require("documents:write"))):
    ctx = agent_context(db, user)
    try:
        event, fresh = pipeline.create_run(ctx, body)
        db.commit()
    except IntegrityError:
        db.rollback()
        try:
            event, fresh = pipeline.create_run(ctx, body)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    if fresh:
        tasks.add_task(execute, event.id, user.id)
    return pipeline.run_view(event)


@router.post("/voice", status_code=202)
def voice_report(tasks: BackgroundTasks, file: UploadFile = File(...), object_id: uuid.UUID | None = Form(None),
                 request_id: uuid.UUID = Form(...), db: Session = Depends(get_db),
                 user: User = Depends(require("documents:write"))):
    from bis import voice
    from bis.services.storage import get_storage

    data = file.file.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "Голосовое сообщение больше 25 МБ")
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    existing = db.get(IntakeEvent, request_id)
    if existing:
        existing = owned(db, user, request_id)
        if existing.result.get("audio_sha256") != digest or existing.result["input"].get("object_id") != (str(object_id) if object_id else None):
            raise HTTPException(409, "Ключ запроса уже использован для другой записи")
        return pipeline.run_view(existing)
    try:
        text = voice.transcribe(data, file.filename or "voice.webm", file.content_type)
    except voice.VoiceError as e:
        raise HTTPException(503, str(e)) from e
    if not text.strip():
        raise HTTPException(400, "В записи не распознана речь")
    event, fresh = pipeline.create_run(agent_context(db, user), pipeline.RunInput(text=text, object_id=object_id, request_id=request_id))
    if fresh:
        event.kind, event.file_name, event.mime = "voice", file.filename or "voice.webm", file.content_type
        event.file_key = get_storage().put(data, event.file_name, prefix=f"intake/{user.organization_id}")
        event.result = {**event.result, "audio_sha256": digest}
    db.commit()
    if fresh:
        tasks.add_task(execute, event.id, user.id)
    return pipeline.run_view(event)


@router.get("")
def list_runs(db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    stmt = select(IntakeEvent).where(IntakeEvent.organization_id == user.organization_id,
        IntakeEvent.result["pipeline"].astext == str(pipeline.VERSION))
    scope = db.info.get('source_context')
    if scope and scope.object_id:
        stmt = stmt.where(IntakeEvent.object_id == scope.object_id)
    events = db.scalars(stmt.order_by(IntakeEvent.created_at.desc()).limit(30)).all()
    return [pipeline.run_view(e) for e in events]


@router.get("/{run_id}")
def get_run(run_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    return pipeline.run_view(owned(db, user, run_id))


@router.get("/{run_id}/readiness")
def readiness(run_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    from bis.services.executive_readiness import review

    return review(agent_context(db, user), owned(db, user, run_id))


@router.post("/{run_id}/resume", status_code=202)
def resume(run_id: uuid.UUID, body: pipeline.Resolution, tasks: BackgroundTasks, db: Session = Depends(get_db),
           user: User = Depends(require("documents:write"))):
    event = owned(db, user, run_id, lock=True)
    if event.result.get("status") in ("running", "queued"):
        raise HTTPException(409, "Обработка уже выполняется")
    if event.result.get("revision") != body.revision:
        raise HTTPException(409, "Задание изменилось. Обновите результат перед исправлением.")
    if event.document_id:
        doc = db.get(Document, event.document_id)
        if doc and doc.status not in ("draft", "rejected"):
            raise HTTPException(409, "Акт уже согласован; создайте новое задание")
    patch = body.model_dump(mode="json", exclude_none=True, exclude={"revision", "choices"})
    if patch.get("object_id") and event.document_id and patch["object_id"] != str(event.object_id):
        raise HTTPException(409, "Для другого объекта создайте новое задание")
    resolutions = {**event.result.get("resolutions", {}), **patch}
    choices = dict(resolutions.get("choices") or {})
    for key, value in body.choices.items():
        if value is None:
            choices.pop(key, None)
        else:
            choices[key] = str(value)
    resolutions["choices"] = choices
    event.result = {**event.result, "status": "queued", "revision": body.revision + 1, "resolutions": resolutions,
        "error": None, "resolution_history": [*event.result.get("resolution_history", []),
        {"by": str(user.id), "at": dt.datetime.now(dt.UTC).isoformat(), "values": patch, "choices": {k: str(v) if v else None for k, v in body.choices.items()}}]}
    event.status = "new"
    db.commit()
    tasks.add_task(execute, event.id, user.id)
    return pipeline.run_view(event)


@router.get("/{run_id}/bundle")
def download_bundle(run_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    event = owned(db, user, run_id)
    try:
        data = pipeline.bundle(agent_context(db, user), event)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="ID_{run_id}.zip"'})
