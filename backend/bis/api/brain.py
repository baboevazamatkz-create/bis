"""«Синапс» — мозг объекта: охват, досчёт, поиск по карточкам, карточка документа со связями."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
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
    from bis.brain.build import build_missing, index_missing, reread_missing, see_missing
    from bis.llm.gateway import get_gateway
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    out = index_missing(db, get_gateway(), get_embedder(), get_storage(), user.organization_id, limit=100)
    db.commit()
    out |= reread_missing(db, get_gateway(), get_embedder(), get_storage(), user.organization_id, limit=10)
    db.commit()
    out |= build_missing(db, get_gateway(), get_embedder(), user.organization_id, limit=100)
    db.commit()
    out |= see_missing(db, get_gateway(), get_embedder(), get_storage(), user.organization_id, limit=10)
    db.commit()
    return out


@router.get("/search")
def search(q: str = "", kind: str | None = None, limit: int = 20, db: Session = Depends(get_db),
           user: User = Depends(require("documents:read")), workspace: Literal["platform", "executive"] = "platform"):
    from bis.agents.builtin_tools import FindDocumentsArgs, find_documents

    ctx = agent_context(db, user, query_only=True)
    res = find_documents.handler(ctx, FindDocumentsArgs(query=q or None, kind=kind or None, limit=min(limit, 30), workspace=workspace))
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


# --- самопроверка ----------------------------------------------------------------------------------

_RUNS: dict[str, dict] = {}  # организация → ход проверки (в памяти процесса)


class SelfCheckIn(BaseModel):
    auto: int = Field(40, ge=0, le=150)
    questions: str = ""  # свои вопросы: «вопрос → ответ», по одному на строку


@router.post("/selfcheck")
def selfcheck_start(body: SelfCheckIn, user: User = Depends(require("documents:read"))):
    """Запустить самопроверку фоном: вопросы по архиву (и свои), ответы ассистента, процент верных."""
    import threading

    from bis.db import SessionLocal

    key = str(user.organization_id)
    if _RUNS.get(key, {}).get("running"):
        raise HTTPException(409, "Проверка уже идёт")
    state = _RUNS[key] = {"running": True, "done": 0, "total": 0, "error": None}
    user_id, org_id = user.id, user.organization_id

    def work() -> None:
        from bis.brain import selfcheck
        from bis.models import Organization

        try:
            with SessionLocal() as db:
                me = db.get(User, user_id)
                cases = selfcheck.parse_questions(body.questions) + selfcheck.generate(db, org_id, body.auto)
                state["total"] = len(cases)
                report = selfcheck.run(db, me, cases, progress=lambda i, n: state.update(done=i))
                org = db.get(Organization, org_id)
                org.settings = {**(org.settings or {}), "brain_selfcheck": report}
                db.commit()
                _save_file(report)
        except Exception as e:  # noqa: BLE001
            state["error"] = str(e)[:300]
        finally:
            state["running"] = False

    threading.Thread(target=work, daemon=True, name="bis-selfcheck").start()
    return state


@router.get("/selfcheck")
def selfcheck_status(db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    from bis.models import Organization

    org = db.get(Organization, user.organization_id)
    return {**_RUNS.get(str(user.organization_id), {"running": False}),
            "report": (org.settings or {}).get("brain_selfcheck") if org else None}


def _save_file(report: dict) -> None:
    """Копия отчёта рядом с BIS (C:\\BIS\\eval) — её удобно переслать целиком."""
    import json

    from bis.cli import _local_home

    home = _local_home()
    if home is None:
        return
    out = home / "eval"
    out.mkdir(exist_ok=True)
    (out / f"selfcheck-{report['at'].replace(':', '-')}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
