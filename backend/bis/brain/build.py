"""Построение «Оси»: карточка и связи документа при загрузке; фоновый досчёт для уже загруженного архива."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from bis.brain import links
from bis.brain.card import CARD_VERSION, Card, build_card
from bis.jobs import job
from bis.models import Document, KnowledgeChunk, KnowledgeSource, Organization

log = logging.getLogger(__name__)
# вид документа в реестре → тип для карточки (если тип при загрузке не сохранён)
KIND_TO_TYPE = {"incoming_letter": "letter", "supply_doc": "upd", "photo": "site_photo"}


def document_text(db: Session, doc: Document) -> str:
    parts = db.scalars(select(KnowledgeChunk.content).join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
                       .where(KnowledgeSource.document_id == doc.id).order_by(KnowledgeChunk.ord).limit(80)).all()
    return "\n".join(parts)


def apply_card(db: Session, embedder, doc: Document, card: Card, *, reembed: bool = True) -> None:
    """Сохранить карточку в документ; шапку карточки — в поиск (фрагменты находятся по смыслу документа целиком)."""
    doc.data = {**(doc.data or {}), "card": card.model_dump(mode="json"), "card_text": card.search_text()[:4000]}
    doc.number = doc.number or (card.number or None)
    if card.date and not doc.date:
        doc.date = dt.date.fromisoformat(card.date)
    for src in db.scalars(select(KnowledgeSource).where(KnowledgeSource.document_id == doc.id)):
        if (src.meta or {}).get("context") == card.title:
            continue
        src.meta = {**(src.meta or {}), "context": card.title}
        if reembed and embedder is not None and getattr(embedder, "semantic", False):
            from bis.rag.ingest import chunk_head, embedding_text
            from bis.rag.search import invalidate_vectors

            chunks = db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.source_id == src.id)).all()
            vecs = embedder.embed_documents([embedding_text(chunk_head(src), c.section_path, c.heading, c.content)
                                             for c in chunks]) if chunks else []
            for c, v in zip(chunks, vecs, strict=True):
                c.embedding = v
            invalidate_vectors()


def build_document(db: Session, gateway, embedder, doc: Document, text: str | None = None) -> Card:
    text = text if text is not None else document_text(db, doc)
    kind = (doc.data or {}).get("detected_type") or KIND_TO_TYPE.get(doc.kind, doc.kind)
    card = build_card(gateway, text, kind, doc.file_name or "")
    apply_card(db, embedder, doc, card)
    links.rebuild(db, doc, card)
    return card


def missing(db: Session, org_id: uuid.UUID, limit: int = 50) -> list[Document]:
    """Документы с текстом в базе знаний, у которых нет карточки или она старой версии."""
    has_text = select(KnowledgeSource.document_id).where(KnowledgeSource.document_id.is_not(None))
    return list(db.scalars(select(Document).where(
        Document.organization_id == org_id, Document.id.in_(has_text),
        or_(Document.data["card"].is_(None), Document.data["card"]["version"].astext != str(CARD_VERSION)))
        .order_by(Document.created_at).limit(limit)))


def build_missing(db: Session, gateway, embedder, org_id: uuid.UUID, limit: int = 50) -> dict[str, Any]:
    done, failed = 0, 0
    for doc in missing(db, org_id, limit):
        try:
            with db.begin_nested():
                build_document(db, gateway, embedder, doc)
            done += 1
        except Exception as e:  # noqa: BLE001 — один документ не останавливает остальные
            failed += 1
            log.warning("Ось: карточка документа %s не построена: %s", doc.id, e)
    return {"cards": done, "failed": failed, "left": len(missing(db, org_id, 1000))}


def coverage(db: Session, org_id: uuid.UUID) -> dict[str, Any]:
    from sqlalchemy import func

    from bis.models import DocumentLink

    total = db.scalar(select(func.count()).select_from(Document).where(Document.organization_id == org_id)) or 0
    cards = db.scalar(select(func.count()).select_from(Document).where(
        Document.organization_id == org_id, Document.data["card"].is_not(None))) or 0
    model = db.scalar(select(func.count()).select_from(Document).where(
        Document.organization_id == org_id, Document.data["card"]["by"].astext == "rules+model")) or 0
    vision = db.scalar(select(func.count()).select_from(Document).where(
        Document.organization_id == org_id, Document.data["card"]["vision"].is_not(None),
        Document.data["card"]["vision"].astext != "null")) or 0
    links_total = db.scalar(select(func.count()).select_from(DocumentLink).where(DocumentLink.organization_id == org_id)) or 0
    links_found = db.scalar(select(func.count()).select_from(DocumentLink).where(
        DocumentLink.organization_id == org_id, DocumentLink.to_document_id.is_not(None))) or 0
    pages = db.scalar(select(func.count()).select_from(KnowledgeChunk).where(
        KnowledgeChunk.organization_id == org_id, KnowledgeChunk.page.is_not(None))) or 0
    return {"documents": total, "cards": cards, "cards_by_model": model, "vision": vision, "links": links_total,
            "links_found": links_found, "chunks_with_page": pages, "pending": len(missing(db, org_id, 1000))}


@job("brain", "Ось: карточки и связи документов", every_minutes=10)
def brain_job(db: Session, org: Organization, today: dt.date) -> dict[str, Any]:
    from bis.llm.gateway import get_gateway
    from bis.rag.embeddings import get_embedder

    if not missing(db, org.id, 1):
        return {"cards": 0}
    return build_missing(db, get_gateway(), get_embedder(), org.id, limit=40)
