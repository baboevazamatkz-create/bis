"""«Эрудит» — мозг объекта: охват, досчёт, поиск по карточкам, карточка документа со связями."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from bis.api.deps import agent_context, require
from bis.db import get_db
from bis.models import Document, User

router = APIRouter(prefix="/brain", tags=["brain"])


@router.get("/coverage")
def coverage(db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    from bis.brain.build import coverage as cov

    return cov(db, user.organization_id)


@router.post("/build")
def build_now(db: Session = Depends(get_db), user: User = Depends(require("documents:write"))):
    """Досчитать сейчас: карточки и связи (до 100 документов), зрение по листам (до 10)."""
    from bis.brain.build import build_missing, see_missing
    from bis.llm.gateway import get_gateway
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    out = build_missing(db, get_gateway(), get_embedder(), user.organization_id, limit=100)
    db.commit()
    out |= see_missing(db, get_gateway(), get_embedder(), get_storage(), user.organization_id, limit=10)
    db.commit()
    return out


@router.get("/search")
def search(q: str = "", kind: str | None = None, limit: int = 20, db: Session = Depends(get_db),
           user: User = Depends(require("documents:read"))):
    from bis.agents.builtin_tools import FindDocumentsArgs, find_documents

    ctx = agent_context(db, user)
    res = find_documents.handler(ctx, FindDocumentsArgs(query=q or None, kind=kind or None, limit=min(limit, 30)))
    return res["documents"]


@router.get("/documents/{doc_id}")
def card(doc_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    from bis.brain.links import neighbours

    doc = db.get(Document, doc_id)
    if doc is None or doc.organization_id != user.organization_id:
        raise HTTPException(404, "Документ не найден")
    return {"id": str(doc.id), "title": doc.title, "kind": doc.kind, "file_name": doc.file_name,
            "date": doc.date.isoformat() if doc.date else None, "card": (doc.data or {}).get("card"),
            "text_from": (doc.data or {}).get("text_from"), **neighbours(db, doc)}
