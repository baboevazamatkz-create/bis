"""Построение «Эрудита»: карточка и связи документа при загрузке; фоновый досчёт для уже загруженного архива."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from bis.brain import links
from bis.brain.card import CARD_VERSION, Card, build_card
from bis.jobs import job
from bis.models import Document, KnowledgeChunk, KnowledgeSource, Organization

log = logging.getLogger(__name__)
# вид документа в реестре → тип для карточки (если тип при загрузке не сохранён)
KIND_TO_TYPE = {"incoming_letter": "letter", "supply_doc": "upd", "photo": "site_photo"}


def document_text(db: Session, doc: Document, tail: bool = False) -> str:
    """Текст документа из базы знаний: начало (80 фрагментов), а с tail — и конец (реквизиты, подписи)."""
    q = (select(KnowledgeChunk.content).join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
         .where(KnowledgeSource.document_id == doc.id))
    parts = db.scalars(q.order_by(KnowledgeChunk.ord).limit(80)).all()
    if tail and len(parts) == 80:
        parts += list(reversed(db.scalars(q.order_by(KnowledgeChunk.ord.desc()).limit(20)).all()))
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
            log.warning("Эрудит: карточка документа %s не построена: %s", doc.id, e)
    return {"cards": done, "failed": failed, "left": len(missing(db, org_id, 1000))}


# --- дочитать сканы: раньше распознавание обрывалось на 20-й странице --------------------------------


def unread_scans(db: Session, org_id: uuid.UUID, limit: int = 5) -> list[Document]:
    """Сканы PDF, у которых могли быть прочитаны не все страницы (ещё не сверены с числом страниц файла)."""
    return list(db.scalars(select(Document).where(
        Document.organization_id == org_id, Document.file_key.is_not(None), Document.file_name.ilike("%.pdf"),
        Document.data["text_from"].astext == "ocr", Document.data["pages_checked"].is_(None))
        .order_by(Document.created_at).limit(limit)))


def reread_scan(db: Session, gateway, embedder, storage, doc: Document) -> int:
    """Распознать скан заново целиком, если прочитано меньше страниц, чем в файле. Возвращает число страниц."""
    import io

    from pypdf import PdfReader

    from bis import ocr
    from bis.rag.ingest import ingest_pages
    from bis.rag.parsers import Page

    data = storage.get(doc.file_key)
    total = len(PdfReader(io.BytesIO(data)).pages)
    read = db.scalar(select(func.max(KnowledgeChunk.page)).join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
                     .where(KnowledgeSource.document_id == doc.id)) or 0
    # без номеров страниц (старая загрузка) прочитано было не больше 20 страниц
    need = total > 20 if read == 0 else total > read
    if need:
        texts = ocr.recognize_pages(data, "application/pdf")
        from bis.rag.columns import split_columns

        pages = [Page(i + 1, split_columns(t)) for i, t in enumerate(texts) if t.strip()]
        if pages:
            kind = next((s.kind for s in db.scalars(select(KnowledgeSource).where(KnowledgeSource.document_id == doc.id))),
                        "org_document")
            for src in db.scalars(select(KnowledgeSource).where(KnowledgeSource.document_id == doc.id)):
                if not (src.meta or {}).get("vision"):
                    db.delete(src)
            db.flush()
            card = (doc.data or {}).get("card") or {}
            ingest_pages(db, embedder, org_id=doc.organization_id, pages=pages, kind=kind, title=doc.title,
                         document_id=doc.id, context=card.get("title"))
            from bis.rag.search import invalidate_vectors

            invalidate_vectors()
            build_document(db, gateway, embedder, doc, text="\n".join(p.text for p in pages))
    doc.data = {**(doc.data or {}), "pages_checked": total}
    return total


def reread_missing(db: Session, gateway, embedder, storage, org_id: uuid.UUID, limit: int = 5) -> dict[str, Any]:
    from bis import ocr

    if not ocr.available():
        return {}
    done = 0
    for doc in unread_scans(db, org_id, limit):
        try:
            with db.begin_nested():
                reread_scan(db, gateway, embedder, storage, doc)
            done += 1
        except Exception as e:  # noqa: BLE001
            log.warning("Эрудит: скан %s не перечитан: %s", doc.id, e)
            doc.data = {**(doc.data or {}), "pages_checked": -1}
    return {"reread": done, "reread_left": len(unread_scans(db, org_id, 1000))}


# --- зрение: схемы, чертежи, сканы листов ----------------------------------------------------------


def vision_missing(db: Session, org_id: uuid.UUID, limit: int = 5) -> list[Document]:
    from bis.brain.vision import VISION_KINDS

    return list(db.scalars(select(Document).where(
        Document.organization_id == org_id, Document.file_key.is_not(None),
        or_(Document.kind.in_(VISION_KINDS), Document.data["card"]["kind"].astext.in_(["scheme", "project_doc", "survey"]),
            Document.data["text_from"].astext == "none"),  # без текста (чертёж, плохой скан) — прочитать можно только глазами
        or_(Document.data["card"]["vision"].is_(None), Document.data["card"]["vision"].astext == "null"),
        or_(Document.file_name.ilike("%.pdf"), Document.file_name.ilike("%.jpg"), Document.file_name.ilike("%.jpeg"),
            Document.file_name.ilike("%.png")))
        .order_by(Document.created_at).limit(limit)))


def see_document(db: Session, gateway, embedder, storage, doc: Document) -> dict[str, Any] | None:
    """Прочитать листы документа зрением и добавить в карточку и поиск. None — зрение не подключено."""
    from bis.brain import vision
    from bis.rag.ingest import ingest_pages
    from bis.rag.parsers import Page

    if not vision.available(gateway):
        return None
    card = Card.model_validate((doc.data or {}).get("card") or {"kind": doc.kind})
    try:
        images = vision.sheet_images(storage.get(doc.file_key), doc.mime, doc.file_name or "")
        sheets = vision.read_sheets(gateway, images)
    except vision.VisionUnavailable:
        return None  # доступа к модели нет — документ ждёт, когда зрение включат
    except Exception as e:  # noqa: BLE001 — отметить, чтобы не повторять каждые 10 минут
        card.vision = {"error": str(e)[:300], "at": dt.datetime.now(dt.UTC).isoformat()}
        doc.data = {**(doc.data or {}), "card": card.model_dump(mode="json")}
        return card.vision
    if not sheets:
        card.vision = {"sheets": [], "note": "модель не прочитала листы"}
    else:
        first = sheets[0].title_block
        card.rd_code = card.rd_code or first.code
        card.revision = card.revision or first.revision
        card.sheets = card.sheets or [s.title_block.sheet for s in sheets if s.title_block.sheet]
        from bis.brain.card import _uniq

        card.elements = _uniq([*card.elements, *(e for s in sheets for e in s.elements)], 10)
        card.axes = _uniq([*card.axes, *(a for s in sheets for a in s.axes)], 12)
        card.levels = _uniq([*card.levels, *(lv for s in sheets for lv in s.levels)], 12)
        if not card.summary or card.by == "rules":
            card.summary = sheets[0].description[:300]
        card.vision = {"sheets": [s.model_dump(mode="json") for s in sheets], "pages": len(images)}
        card.title = card.label()
        for src in db.scalars(select(KnowledgeSource).where(KnowledgeSource.document_id == doc.id)):
            if (src.meta or {}).get("vision"):
                db.delete(src)
        db.flush()
        src = ingest_pages(db, embedder, org_id=doc.organization_id, kind="project_doc",
                           title=f"{doc.title} — описание листов", document_id=doc.id, context=card.title,
                           pages=[Page(i + 1, vision.sheet_text(i + 1, s)) for i, s in enumerate(sheets)])
        src.meta = {**(src.meta or {}), "vision": True}
        from bis.rag.search import invalidate_vectors

        invalidate_vectors()
    apply_card(db, embedder, doc, card)
    links.rebuild(db, doc, card)
    return card.vision


def see_missing(db: Session, gateway, embedder, storage, org_id: uuid.UUID, limit: int = 5) -> dict[str, Any]:
    from bis.brain import vision

    if not vision.available(gateway):
        return {"vision": "не подключено", "waiting": len(vision_missing(db, org_id, 1000))}
    seen = 0
    for doc in vision_missing(db, org_id, limit):
        with db.begin_nested():
            if see_document(db, gateway, embedder, storage, doc) is not None:
                seen += 1
        if not vision.available(gateway):  # модель перестала отвечать — остальное подождёт
            break
    return {"vision": seen, "waiting": len(vision_missing(db, org_id, 1000)), "vision_status": vision.status(gateway)}


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
    from bis.brain import vision as vis
    from bis.llm.gateway import get_gateway

    return {"documents": total, "cards": cards, "cards_by_model": model, "vision": vision, "links": links_total,
            "links_found": links_found, "chunks_with_page": pages, "pending": len(missing(db, org_id, 1000)),
            "vision_waiting": len(vision_missing(db, org_id, 1000)), "vision_available": vis.available(get_gateway()),
            "vision_status": vis.status(get_gateway())}


@job("brain", "Эрудит: карточки и связи документов", every_minutes=10)
def brain_job(db: Session, org: Organization, today: dt.date) -> dict[str, Any]:
    from bis.llm.gateway import get_gateway
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    gateway, embedder = get_gateway(), get_embedder()
    out: dict[str, Any] = {"cards": 0}
    if unread_scans(db, org.id, 1):
        out |= reread_missing(db, gateway, embedder, get_storage(), org.id, limit=5)
    if missing(db, org.id, 1):
        out = build_missing(db, gateway, embedder, org.id, limit=40)
    if vision_missing(db, org.id, 1):
        out |= see_missing(db, gateway, embedder, get_storage(), org.id, limit=5)
    from bis.brain.parties import link_all

    out["contract_parties"] = link_all(db, org.id)
    return out
