"""Организация, пользователи, память организации, аудит, состояние системы."""


import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from bis import __version__
from bis.api.deps import require
from bis.api.schemas import MemoryIn, MemoryOut, OrganizationOut, OrganizationUpdate, UserIn, UserOut, partial
from bis.db import get_db
from bis.llm.gateway import get_gateway
from bis.models import AuditLog, Organization, OrgMemory, User
from bis.plugins.sdk import get_registry
from bis.security import ROLES, hash_password
from bis.services.audit import audit

router = APIRouter(tags=["admin"])


@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    gw = get_gateway()
    reg = get_registry()
    return {
        "status": "ok",
        "version": __version__,
        "llm_providers": {n: e.max_sensitivity for n, e in gw.entries.items()},
        "plugins": list(reg.plugins),
        "agents": list(reg.agents),
    }


@router.get("/org", response_model=OrganizationOut)
def get_org(db: Session = Depends(get_db), user: User = Depends(require("registry:read"))):
    return db.get(Organization, user.organization_id)


@router.patch("/org", response_model=OrganizationOut)
def update_org(body: OrganizationUpdate, db: Session = Depends(get_db), user: User = Depends(require("settings:write"))):
    org = db.get(Organization, user.organization_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(org, k, v)
    audit(db, org.id, actor_type="user", actor=str(user.id), action="org.update", entity_type="organization",
          entity_id=org.id, details={"fields": list(body.model_dump(exclude_unset=True))})
    db.commit()
    return org


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), user: User = Depends(require("users:read"))):
    return db.scalars(select(User).where(User.organization_id == user.organization_id).order_by(User.full_name)).all()


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(body: UserIn, db: Session = Depends(get_db), user: User = Depends(require("users:write"))):
    if body.role not in ROLES:
        raise HTTPException(400, f"Неизвестная роль. Допустимо: {', '.join(ROLES)}")
    if db.scalar(select(User).where(func.lower(User.email) == body.email.lower())):
        raise HTTPException(409, "Пользователь с таким email уже существует")
    new = User(organization_id=user.organization_id, email=body.email.lower(), full_name=body.full_name,
               position=body.position, role=body.role, password_hash=hash_password(body.password))
    db.add(new)
    db.flush()
    audit(db, user.organization_id, actor_type="user", actor=str(user.id), action="user.create",
          entity_type="user", entity_id=new.id, details={"role": body.role})
    db.commit()
    return new


@router.get("/memory", response_model=list[MemoryOut])
def list_memory(db: Session = Depends(get_db), user: User = Depends(require("registry:read"))):
    return db.scalars(select(OrgMemory).where(OrgMemory.organization_id == user.organization_id)
                      .order_by(OrgMemory.created_at.desc())).all()


@router.post("/memory", response_model=MemoryOut, status_code=201)
def add_memory(body: MemoryIn, db: Session = Depends(get_db), user: User = Depends(require("settings:write"))):
    row = OrgMemory(organization_id=user.organization_id, source=f"user:{user.id}", **body.model_dump())
    db.add(row)
    db.commit()
    return row


@router.patch("/memory/{memory_id}", response_model=MemoryOut)
def update_memory(memory_id: uuid.UUID, body: partial(MemoryIn), db: Session = Depends(get_db),  # type: ignore[valid-type]
                  user: User = Depends(require("settings:write"))):
    row = db.get(OrgMemory, memory_id)
    if row is None or row.organization_id != user.organization_id:
        raise HTTPException(404, "Запись не найдена")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    db.commit()
    return row


@router.get("/audit")
def audit_log(limit: int = 200, db: Session = Depends(get_db), user: User = Depends(require("audit:read"))):
    rows = db.scalars(select(AuditLog).where(AuditLog.organization_id == user.organization_id)
                      .order_by(AuditLog.created_at.desc()).limit(min(limit, 1000)))
    return [{"created_at": r.created_at, "actor_type": r.actor_type, "actor": r.actor, "action": r.action,
             "entity_type": r.entity_type, "entity_id": r.entity_id, "details": r.details} for r in rows]


@router.post("/evals/run")
def run_evals(cases: list[dict] | None = None, k: int = 5, db: Session = Depends(get_db),
              user: User = Depends(require("audit:read"))):
    """Оценка поиска и маршрутизации на наборе вопросов (по умолчанию — демо-набор)."""
    import json
    from pathlib import Path

    from bis.evals import EvalCase, run_eval
    from bis.rag.embeddings import get_embedder

    if not cases:
        cases = json.loads((Path(__file__).parents[1] / "demo" / "eval_demo.json").read_text(encoding="utf-8"))
    parsed = [EvalCase.model_validate(c) for c in cases]
    sup = get_registry().supervisor()
    return run_eval(db, get_embedder(), user.organization_id, parsed, k=k, route_fn=lambda q: sup.route(q)[0])


class ResetIn(BaseModel):
    confirm: str


@router.post("/org/reset")
def reset_org(body: ResetIn, db: Session = Depends(get_db), user: User = Depends(require("users:write"))):
    """Очистить все данные организации (кроме реквизитов, пользователей и общих нормативов). Подтверждение: УДАЛИТЬ."""
    from bis.services.reset import reset_organization
    from bis.services.storage import get_storage

    if body.confirm.strip().upper() not in ("УДАЛИТЬ", "DELETE"):
        raise HTTPException(400, "Для подтверждения напишите УДАЛИТЬ")
    res = reset_organization(db, get_storage(), user.organization_id)
    audit(db, user.organization_id, actor_type="user", actor=str(user.id), action="org.reset", details=res)
    db.commit()
    return res
