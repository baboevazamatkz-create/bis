from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from bis.api.deps import require
from bis.db import get_db
from bis.models import KnowledgeChunk, KnowledgeSource, User
from bis.rag.embeddings import get_embedder
from bis.rag.ingest import KNOWLEDGE_KINDS, ingest_bytes
from bis.rag.parsers import UnsupportedFormat
from bis.rag.search import hybrid_search
from bis.services.audit import audit
from bis.services.storage import get_storage

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
MAX_UPLOAD = 100 * 1024 * 1024


class SourceOut(BaseModel):
    id: uuid.UUID
    kind: str
    title: str
    designation: str | None
    status: str
    effective_date: date | None
    file_name: str | None
    chunk_count: int
    is_global: bool
    meta: dict


class SourceUpdate(BaseModel):
    title: str | None = None
    designation: str | None = None
    status: Literal["active", "cancelled", "replaced", "unknown"] | None = None
    effective_date: date | None = None


def _out(s: KnowledgeSource) -> SourceOut:
    return SourceOut(id=s.id, kind=s.kind, title=s.title, designation=s.designation, status=s.status,
                     effective_date=s.effective_date, file_name=s.file_name, chunk_count=s.chunk_count,
                     is_global=s.organization_id is None, meta=s.meta or {})


def _owned(db: Session, user: User, source_id: uuid.UUID) -> KnowledgeSource:
    src = db.get(KnowledgeSource, source_id)
    if src is None or (src.organization_id not in (None, user.organization_id)):
        raise HTTPException(404, "Источник не найден")
    if src.organization_id is None and not user.is_platform_admin:
        raise HTTPException(403, "Общую нормативную библиотеку изменяет администратор платформы")
    return src


@router.get("/kinds")
def kinds():
    return KNOWLEDGE_KINDS


@router.post("/upload", response_model=SourceOut, status_code=201)
def upload(
    file: UploadFile = File(...),
    kind: str = Form("org_document"),
    title: str | None = Form(None),
    designation: str | None = Form(None),
    status: str = Form("active"),
    scope: Literal["organization", "global"] = Form("organization"),
    db: Session = Depends(get_db),
    user: User = Depends(require("knowledge:write")),
):
    if scope == "global" and not user.is_platform_admin:
        raise HTTPException(403, "Загрузка в общую библиотеку доступна администратору платформы")
    data = file.file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Файл больше 100 МБ")
    try:
        src = ingest_bytes(
            db, get_storage(), get_embedder(), org_id=None if scope == "global" else user.organization_id,
            data=data, filename=file.filename or "file", kind=kind, title=title, designation=designation,
            status=status, mime=file.content_type,
        )
    except UnsupportedFormat as e:
        raise HTTPException(415, str(e)) from e
    audit(db, user.organization_id, actor_type="user", actor=str(user.id), action="knowledge.upload",
          entity_type="knowledge_source", entity_id=src.id, details={"kind": kind, "file": file.filename})
    db.commit()
    return _out(src)


@router.get("/sources", response_model=list[SourceOut])
def list_sources(kind: str | None = None, q: str | None = None, limit: int = 200, db: Session = Depends(get_db),
                 user: User = Depends(require("knowledge:read"))):
    stmt = select(KnowledgeSource).where(
        or_(KnowledgeSource.organization_id == user.organization_id, KnowledgeSource.organization_id.is_(None))
    )
    if kind:
        stmt = stmt.where(KnowledgeSource.kind == kind)
    if q:
        stmt = stmt.where(or_(KnowledgeSource.title.ilike(f"%{q}%"), KnowledgeSource.designation.ilike(f"%{q}%")))
    return [_out(s) for s in db.scalars(stmt.order_by(KnowledgeSource.created_at.desc()).limit(min(limit, 1000)))]


@router.patch("/sources/{source_id}", response_model=SourceOut)
def update_source(source_id: uuid.UUID, body: SourceUpdate, db: Session = Depends(get_db),
                  user: User = Depends(require("knowledge:write"))):
    src = _owned(db, user, source_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(src, k, v)
    audit(db, user.organization_id, actor_type="user", actor=str(user.id), action="knowledge.update",
          entity_type="knowledge_source", entity_id=src.id, details=body.model_dump(mode="json", exclude_unset=True))
    db.commit()
    return _out(src)


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: uuid.UUID, db: Session = Depends(get_db),
                  user: User = Depends(require("knowledge:write"))):
    src = _owned(db, user, source_id)
    if src.file_key:
        get_storage().delete(src.file_key)
    db.delete(src)
    audit(db, user.organization_id, actor_type="user", actor=str(user.id), action="knowledge.delete",
          entity_type="knowledge_source", entity_id=source_id)
    db.commit()


@router.get("/sources/{source_id}/chunks")
def source_chunks(source_id: uuid.UUID, limit: int = 50, offset: int = 0, db: Session = Depends(get_db),
                  user: User = Depends(require("knowledge:read"))):
    src = db.get(KnowledgeSource, source_id)
    if src is None or src.organization_id not in (None, user.organization_id):
        raise HTTPException(404, "Источник не найден")
    rows = db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.source_id == source_id)
                      .order_by(KnowledgeChunk.ord).limit(min(limit, 500)).offset(offset))
    return [{"ord": c.ord, "clause": c.clause, "section_path": c.section_path, "heading": c.heading,
             "page": c.page, "content": c.content} for c in rows]


@router.get("/search")
def search(q: str, scope: Literal["norms", "organization", "all"] = "all", k: int = 8,
           db: Session = Depends(get_db), user: User = Depends(require("knowledge:read"))):
    from bis.agents.builtin_tools import SCOPE_KINDS

    hits = hybrid_search(db, get_embedder(), q, org_id=user.organization_id, kinds=SCOPE_KINDS[scope], k=min(k, 30))
    return {"query": q, "results": [h.to_dict() for h in hits]}
