from __future__ import annotations

import contextlib
import mimetypes
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.agents.base import AgentContext
from bis.agents.builtin_tools import AOSRArgs, LetterArgs, create_aosr_draft, create_letter_draft
from bis.api.deps import agent_context, require
from bis.api.schemas import DocumentOut
from bis.db import get_db
from bis.documents import DOCUMENT_KINDS, AOSRData, validate_aosr
from bis.documents.aosr import REPRESENTATIVE_ROLES
from bis.models import Document, DocumentTemplate, User
from bis.rag.embeddings import get_embedder
from bis.rag.ingest import ingest_bytes
from bis.rag.parsers import UnsupportedFormat
from bis.services import documents as docsvc
from bis.services.audit import audit
from bis.services.storage import get_storage

router = APIRouter(prefix="/documents", tags=["documents"])
templates_router = APIRouter(prefix="/templates", tags=["templates"])

KNOWLEDGE_KIND_FOR = {"incoming_letter": "letter", "letter": "letter", "contract": "contract", "order": "org_document",
                      "project_doc": "project_doc"}


def _owned(db: Session, user: User, doc_id: uuid.UUID) -> Document:
    doc = db.get(Document, doc_id)
    if doc is None or doc.organization_id != user.organization_id:
        raise HTTPException(404, "Документ не найден")
    return doc


def _form_ctx(db: Session, user: User) -> AgentContext:
    ctx = agent_context(db, user)
    ctx.agent_name = "form"
    return ctx


def _run_tool(t, ctx: AgentContext, args: BaseModel) -> Document:
    try:
        ref = t.handler(ctx, args)
    except (LookupError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    doc = ctx.db.get(Document, uuid.UUID(ref["id"]))
    doc.created_by = f"user:{ctx.user.id}"
    ctx.db.commit()
    return doc


@router.get("/kinds")
def kinds():
    return DOCUMENT_KINDS


@router.get("/representative-roles")
def representative_roles():
    return {k: {"title": v["title"], "nrs": v["nrs"], "required": v["required"]} for k, v in REPRESENTATIVE_ROLES.items()}


@router.get("", response_model=list[DocumentOut])
def list_documents(kind: str | None = None, status: str | None = None, object_id: uuid.UUID | None = None,
                   limit: int = 100, offset: int = 0, db: Session = Depends(get_db),
                   user: User = Depends(require("documents:read"))):
    stmt = select(Document).where(Document.organization_id == user.organization_id)
    if kind:
        stmt = stmt.where(Document.kind == kind)
    if status:
        stmt = stmt.where(Document.status == status)
    if object_id:
        stmt = stmt.where(Document.object_id == object_id)
    return db.scalars(stmt.order_by(Document.created_at.desc()).limit(min(limit, 500)).offset(offset)).all()


@router.get("/{doc_id}", response_model=DocumentOut)
def get_document(doc_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    return _owned(db, user, doc_id)


@router.get("/{doc_id}/file")
def download(doc_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    doc = _owned(db, user, doc_id)
    if not doc.file_key:
        raise HTTPException(404, "У документа нет файла")
    name = doc.file_name or "document"
    return Response(
        content=get_storage().get(doc.file_key),
        media_type=doc.mime or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@router.get("/{doc_id}/preview")
def preview(doc_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    doc = _owned(db, user, doc_id)
    key = (doc.data or {}).get("preview_key")
    if not key:
        raise HTTPException(404, "Нет предпросмотра")
    return Response(content=get_storage().get(key), media_type="image/svg+xml")


@router.get("/{doc_id}/dwg")
def download_dwg(doc_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    doc = _owned(db, user, doc_id)
    key = (doc.data or {}).get("dwg_key")
    if not key:
        raise HTTPException(404, "DWG не сформирован (нужен ODA File Converter на сервере)")
    name = (doc.file_name or "scheme.dxf").rsplit(".", 1)[0] + ".dwg"
    return Response(content=get_storage().get(key), media_type="image/vnd.dwg",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.post("/aosr", response_model=DocumentOut, status_code=201)
def create_aosr(body: AOSRArgs, db: Session = Depends(get_db), user: User = Depends(require("documents:write"))):
    """Формирование АОСР по структурированной форме (без языковой модели)."""
    return _run_tool(create_aosr_draft, _form_ctx(db, user), body)


@router.post("/aosr/check")
def check_aosr(body: AOSRArgs, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    """Проверка данных АОСР до формирования: участники, представители, замечания."""
    try:
        p = docsvc.object_participants(db, user.organization_id, uuid.UUID(body.object_id), body.act_date)
    except (LookupError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    if p["developer"] is None:
        return {"issues": [{"level": "error", "field": "developer", "message": "У объекта не указан застройщик"}]}
    data = AOSRData(object_name=p["object_name"], developer=p["developer"], builder=p["builder"],
                    designer=p["designer"], representatives=p["representatives"],
                    **body.model_dump(exclude={"object_id", "act_date"}), date=body.act_date)
    return {"issues": [i.model_dump() for i in validate_aosr(data)],
            "representatives": {k: v.format() for k, v in p["representatives"].items()}}


@router.post("/letter", response_model=DocumentOut, status_code=201)
def create_letter(body: LetterArgs, db: Session = Depends(get_db), user: User = Depends(require("documents:write"))):
    return _run_tool(create_letter_draft, _form_ctx(db, user), body)


@router.post("/upload", response_model=DocumentOut, status_code=201)
def upload_document(
    file: UploadFile = File(...),
    kind: str = Form("incoming_letter"),
    title: str | None = Form(None),
    number: str | None = Form(None),
    object_id: uuid.UUID | None = Form(None),
    index: bool = Form(True),
    process: bool = Form(True),
    db: Session = Depends(get_db),
    user: User = Depends(require("documents:write")),
):
    """Регистрация входящего/внешнего документа и (по умолчанию) индексация в базе знаний организации."""
    data = file.file.read()
    name = file.filename or "file"
    mime = file.content_type or mimetypes.guess_type(name)[0]
    doc = docsvc.save_document(db, get_storage(), org_id=user.organization_id, kind=kind, title=title or name,
                               data={}, file_bytes=data, file_name=name, created_by=f"user:{user.id}",
                               number=number, object_id=object_id, mime=mime or "application/octet-stream")
    doc.source = "uploaded"
    doc.status = "registered"
    if kind == "project_doc":
        from bis.rag.parsers import parse_file
        from bis.services.project_docs import detect_section

        head = ""
        with contextlib.suppress(UnsupportedFormat, ValueError):
            head = parse_file(data, name, mime).text[:3000]
        doc.data = {**(doc.data or {}), "pd_section": detect_section(f"{doc.title} {name}", head)}
    if index:
        try:
            ingest_bytes(db, get_storage(), get_embedder(), org_id=user.organization_id, data=data, filename=name,
                         kind=KNOWLEDGE_KIND_FOR.get(kind, "org_document"), title=doc.title, document_id=doc.id,
                         mime=mime)
        except UnsupportedFormat:
            doc.issues = [{"level": "warning", "field": "file", "message": "Формат не индексируется в базе знаний"}]
    if kind == "incoming_letter" and process:
        from bis.llm.gateway import get_gateway
        from bis.services.correspondence import process_incoming

        try:
            process_incoming(db, get_storage(), get_gateway(), doc, actor="agent:secretary")
        except UnsupportedFormat:
            doc.issues = [*doc.issues, {"level": "warning", "field": "file", "message": "Текст письма не извлечён — разберите вручную"}]
    db.commit()
    return doc


class RejectIn(BaseModel):
    reason: str


@router.post("/{doc_id}/approve", response_model=DocumentOut)
def approve(doc_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:approve"))):
    doc = _owned(db, user, doc_id)
    try:
        docsvc.approve_document(db, doc, user)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return doc


@router.post("/{doc_id}/reject", response_model=DocumentOut)
def reject(doc_id: uuid.UUID, body: RejectIn, db: Session = Depends(get_db),
           user: User = Depends(require("documents:approve"))):
    doc = _owned(db, user, doc_id)
    docsvc.reject_document(db, doc, user, body.reason)
    db.commit()
    return doc


# --- шаблоны организации ------------------------------------------------------------------------


@templates_router.get("")
def list_templates(db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    rows = db.scalars(select(DocumentTemplate).where(DocumentTemplate.organization_id == user.organization_id))
    return [{"id": t.id, "kind": t.kind, "name": t.name, "is_default": t.is_default} for t in rows]


@templates_router.post("", status_code=201)
def upload_template(file: UploadFile = File(...), kind: str = Form(...), name: str = Form(...),
                          is_default: bool = Form(True), db: Session = Depends(get_db),
                          user: User = Depends(require("settings:write"))):
    if not (file.filename or "").lower().endswith(".docx"):
        raise HTTPException(415, "Шаблон должен быть в формате DOCX")
    data = file.file.read()
    if is_default:
        for t in db.scalars(select(DocumentTemplate).where(DocumentTemplate.organization_id == user.organization_id,
                                                           DocumentTemplate.kind == kind)):
            t.is_default = False
    key = get_storage().put(data, file.filename or "template.docx", prefix=f"templates/{user.organization_id}")
    tpl = DocumentTemplate(organization_id=user.organization_id, kind=kind, name=name, file_key=key,
                           is_default=is_default)
    db.add(tpl)
    db.flush()
    audit(db, user.organization_id, actor_type="user", actor=str(user.id), action="template.upload",
          entity_type="template", entity_id=tpl.id, details={"kind": kind, "name": name})
    db.commit()
    return {"id": tpl.id, "kind": kind, "name": name, "is_default": is_default}


@templates_router.delete("/{template_id}", status_code=204)
def delete_template(template_id: uuid.UUID, db: Session = Depends(get_db),
                    user: User = Depends(require("settings:write"))):
    tpl = db.get(DocumentTemplate, template_id)
    if tpl is None or tpl.organization_id != user.organization_id:
        raise HTTPException(404, "Шаблон не найден")
    get_storage().delete(tpl.file_key)
    db.delete(tpl)
    db.commit()
