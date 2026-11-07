"""Договоры (файл, анализ, импорт), уведомления, сводка контроля, фоновые задачи."""

import datetime as dt
import mimetypes
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from bis.api.deps import require
from bis.db import SessionLocal, get_db
from bis.jobs import load_all_jobs, run_job
from bis.llm.gateway import get_gateway
from bis.models import (
    Contract,
    ContractObligation,
    Document,
    JobRun,
    KnowledgeSource,
    Notification,
    Task,
    User,
)
from bis.rag.embeddings import get_embedder
from bis.rag.ingest import ingest_bytes
from bis.rag.parsers import UnsupportedFormat, parse_file
from bis.services import contracts as csvc
from bis.services.storage import get_storage

router = APIRouter(tags=["control"])


def _contract(db: Session, user: User, contract_id: uuid.UUID) -> Contract:
    c = db.get(Contract, contract_id)
    if c is None or c.organization_id != user.organization_id:
        raise HTTPException(404, "Договор не найден")
    return c


def _analysis_out(c: Contract, data: csvc.ContractExtraction, result, added: int) -> dict:
    return {"contract_id": c.id, "obligations_found": len(data.obligations), "obligations_added": added,
            "risks": data.risks, "penalties": data.penalties, "advance": data.advance, "warranty": data.warranty,
            "used_fallback": result.used_fallback, "provider": result.provider, "errors": result.errors}


def _attach(db: Session, user: User, c: Contract, file: UploadFile) -> bytes:
    data = file.file.read()
    name = file.filename or "contract"
    mime = file.content_type or mimetypes.guess_type(name)[0]
    doc = csvc.attach_contract_file(db, get_storage(), c, data, name, mime, actor=f"user:{user.id}")
    try:
        ingest_bytes(db, get_storage(), get_embedder(), org_id=user.organization_id, data=data, filename=name,
                     kind="contract", title=f"Договор № {c.number}", document_id=doc.id, mime=mime)
    except UnsupportedFormat as e:
        raise HTTPException(415, str(e)) from e
    return data


@router.post("/contracts/{contract_id}/file")
def upload_contract_file(contract_id: uuid.UUID, file: UploadFile = File(...), analyze: bool = Form(True),
                               db: Session = Depends(get_db), user: User = Depends(require("registry:write"))):
    c = _contract(db, user, contract_id)
    _attach(db, user, c, file)
    out: dict = {"contract_id": c.id, "document_id": c.document_id}
    if analyze:
        data, result, added = csvc.analyze_contract(db, get_storage(), get_gateway(), c, actor=f"user:{user.id}")
        out = _analysis_out(c, data, result, added)
    db.commit()
    return out


@router.post("/contracts/{contract_id}/analyze")
def analyze(contract_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("registry:write"))):
    c = _contract(db, user, contract_id)
    try:
        data, result, added = csvc.analyze_contract(db, get_storage(), get_gateway(), c, actor=f"user:{user.id}")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return _analysis_out(c, data, result, added)


@router.post("/contracts/import", status_code=201)
def import_contract(file: UploadFile = File(...), kind: str = Form("general"), our_role: str = Form("contractor"),
                          object_id: uuid.UUID | None = Form(None), db: Session = Depends(get_db),
                          user: User = Depends(require("registry:write"))):
    """Новый договор из файла: реквизиты и обязательства извлекаются автоматически."""
    data = file.file.read()
    try:
        text = parse_file(data, file.filename or "contract", file.content_type).text
    except UnsupportedFormat as e:
        raise HTTPException(415, str(e)) from e
    c = Contract(organization_id=user.organization_id, kind=kind, our_role=our_role, number="б/н", object_id=object_id)
    db.add(c)
    db.flush()
    file.file.seek(0)
    _attach(db, user, c, file)
    extracted, result, added = csvc.analyze_contract(db, get_storage(), get_gateway(), c, actor=f"user:{user.id}",
                                                     text=text)
    if extracted.number:
        c.number = extracted.number
        title = f"Договор № {c.number}"
        db.get(Document, c.document_id).title = title
        for src in db.scalars(select(KnowledgeSource).where(KnowledgeSource.document_id == c.document_id)):
            src.title = title
    db.commit()
    return _analysis_out(c, extracted, result, added) | {"number": c.number}


class PenaltyIn(BaseModel):
    base_amount: float
    days: int
    rate_percent_per_day: float | None = None
    key_rate_percent: float | None = None
    key_rate_fraction: str | None = None
    cap_percent: float | None = None


@router.post("/contracts/penalty")
def penalty(body: PenaltyIn, user: User = Depends(require("registry:read"))):
    try:
        return csvc.calc_penalty(**body.model_dump())
    except (ValueError, ArithmeticError) as e:
        raise HTTPException(400, str(e)) from e


# --- сводка контроля ----------------------------------------------------------------------------


@router.get("/control/summary")
def control_summary(db: Session = Depends(get_db), user: User = Depends(require("registry:read"))):
    today = dt.date.today()
    week = today + dt.timedelta(days=7)
    org = user.organization_id
    active = ["open", "overdue"]

    def count(stmt) -> int:
        return db.scalar(stmt) or 0

    ob = select(func.count()).select_from(ContractObligation).where(ContractObligation.organization_id == org)
    tk = select(func.count()).select_from(Task).where(Task.organization_id == org)
    return {
        "obligations_overdue": count(ob.where(ContractObligation.status.in_(active), ContractObligation.due_date < today)),
        "obligations_week": count(ob.where(ContractObligation.status == "open",
                                           ContractObligation.due_date.between(today, week))),
        "obligations_no_responsible": count(ob.where(ContractObligation.status.in_(active),
                                                     ContractObligation.party == "we",
                                                     ContractObligation.responsible_id.is_(None))),
        "tasks_overdue": count(tk.where(Task.status.in_(["open", "in_progress"]), Task.due_date < today)),
        "tasks_open": count(tk.where(Task.status.in_(["open", "in_progress"]))),
        "notifications_unread": count(select(func.count()).select_from(Notification).where(
            Notification.user_id == user.id, Notification.is_read.is_(False))),
    }


@router.get("/control/risks")
def control_risks(db: Session = Depends(get_db), user: User = Depends(require("registry:read"))):
    """Рейтинг рисков объектов: балл и причины (просрочки, замечания, скрытые работы без АОСР, поставки без сертификатов)."""
    from bis.services.risk import ranking

    return ranking(db, user.organization_id)


@router.get("/control/digest")
def control_digest(date: dt.date | None = None, db: Session = Depends(get_db), user: User = Depends(require("registry:read"))):
    """Сводка дня: сделано, пришло, замечания, ждёт решения, сроки, кто из прорабов не отчитался."""
    from bis.services import digest

    d = digest.build(db, user.organization_id, date or dt.date.today())
    return d | {"text": digest.text(d)}


# --- уведомления --------------------------------------------------------------------------------


@router.get("/notifications")
def notifications(unread_only: bool = False, limit: int = 50, db: Session = Depends(get_db),
                  user: User = Depends(require("knowledge:read"))):
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    rows = db.scalars(stmt.order_by(Notification.created_at.desc()).limit(min(limit, 200)))
    return [{"id": n.id, "created_at": n.created_at, "level": n.level, "title": n.title, "body": n.body,
             "entity_type": n.entity_type, "entity_id": n.entity_id, "is_read": n.is_read} for n in rows]


@router.post("/notifications/read-all", status_code=204)
def read_all(db: Session = Depends(get_db), user: User = Depends(require("knowledge:read"))):
    db.execute(update(Notification).where(Notification.user_id == user.id).values(is_read=True))
    db.commit()


@router.post("/notifications/{notification_id}/read", status_code=204)
def read_one(notification_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("knowledge:read"))):
    n = db.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(404, "Уведомление не найдено")
    n.is_read = True
    db.commit()


# --- фоновые задачи -----------------------------------------------------------------------------


@router.get("/jobs")
def jobs(db: Session = Depends(get_db), user: User = Depends(require("audit:read"))):
    out = []
    for name, j in load_all_jobs().items():
        last = db.scalar(select(JobRun).where(JobRun.name == name).order_by(JobRun.started_at.desc()).limit(1))
        details = (last.details or {}).get(str(user.organization_id)) if last else None
        out.append({"name": name, "title": j.title, "every_minutes": j.every_minutes,
                    "last_run": last.started_at if last else None, "status": last.status if last else None,
                    "result": details})
    return out


@router.post("/jobs/{name}/run")
def run(name: str, user: User = Depends(require("settings:write"))):
    if name not in load_all_jobs():
        raise HTTPException(404, "Задача не найдена")
    r = run_job(SessionLocal, name)
    return {"name": name, "status": r.status, "result": (r.details or {}).get(str(user.organization_id))}

