"""Загрузка документа в базу знаний: хранение файла → извлечение текста → фрагменты → эмбеддинги."""

from __future__ import annotations

import mimetypes
import uuid
from datetime import date

from sqlalchemy import delete
from sqlalchemy.orm import Session

from bis.models import KnowledgeChunk, KnowledgeSource
from bis.rag.chunking import GenericChunker, NormativeChunker, detect_designation
from bis.rag.embeddings import Embedder
from bis.rag.parsers import Page, parse_file
from bis.services.storage import Storage

NORMATIVE_KINDS = {"norm"}
KNOWLEDGE_KINDS = {
    "norm": "Нормативный документ (СП, ГОСТ, СНиП, приказ)",
    "org_document": "Документ организации (регламент, приказ, шаблон)",
    "letter": "Письмо",
    "contract": "Договор",
    "project_doc": "Проектная документация",
}


def embedding_text(designation: str | None, section_path: str | None, heading: str | None, content: str) -> str:
    head = " | ".join(x for x in (designation, section_path, heading) if x)
    return f"{head}\n{content}" if head else content


def ingest_bytes(
    db: Session,
    storage: Storage,
    embedder: Embedder,
    *,
    org_id: uuid.UUID | None,
    data: bytes,
    filename: str,
    kind: str,
    title: str | None = None,
    designation: str | None = None,
    status: str = "active",
    effective_date: date | None = None,
    mime: str | None = None,
    document_id: uuid.UUID | None = None,
) -> KnowledgeSource:
    if kind not in KNOWLEDGE_KINDS:
        raise ValueError(f"Неизвестный тип источника: {kind}")
    mime = mime or mimetypes.guess_type(filename)[0]
    parsed = parse_file(data, filename, mime)
    key = storage.put(data, filename, prefix=f"knowledge/{org_id or 'global'}")
    if kind in NORMATIVE_KINDS and not designation:
        designation = detect_designation(parsed.text, filename)
    source = KnowledgeSource(
        organization_id=org_id,
        kind=kind,
        title=title or parsed.title or filename,
        designation=designation,
        status=status,
        effective_date=effective_date,
        file_key=key,
        file_name=filename,
        mime=mime,
        document_id=document_id,
        meta=parsed.meta,
    )
    db.add(source)
    db.flush()
    index_pages(db, embedder, source, parsed.pages)
    return source


def index_pages(db: Session, embedder: Embedder, source: KnowledgeSource, pages: list[Page]) -> int:
    db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.source_id == source.id))
    if source.kind in NORMATIVE_KINDS:
        chunks = NormativeChunker().chunk(pages, source.designation)
    else:
        chunks = GenericChunker().chunk(pages)
    texts = [embedding_text(source.designation or source.title, c.section_path, c.heading, c.content) for c in chunks]
    from bis.models.knowledge import VECTOR

    # без pgvector векторы не нужны: поиск полнотекстовый и по номерам пунктов
    keep = VECTOR or getattr(embedder, "semantic", False)  # смысловые векторы храним и без pgvector
    vectors = embedder.embed_documents(texts) if texts and keep else [None] * len(texts)
    for c, vec in zip(chunks, vectors, strict=True):
        db.add(
            KnowledgeChunk(
                source_id=source.id,
                organization_id=source.organization_id,
                ord=c.ord,
                clause=c.clause,
                section_path=c.section_path,
                heading=c.heading,
                content=c.content,
                page=c.page,
                embedding=vec,
            )
        )
    source.chunk_count = len(chunks)
    db.flush()
    return len(chunks)


def ingest_text(
    db: Session,
    embedder: Embedder,
    *,
    org_id: uuid.UUID | None,
    text: str,
    kind: str,
    title: str,
    designation: str | None = None,
    document_id: uuid.UUID | None = None,
) -> KnowledgeSource:
    """Индексация текста без файла (например, содержимое сгенерированного документа)."""
    source = KnowledgeSource(
        organization_id=org_id, kind=kind, title=title, designation=designation, document_id=document_id
    )
    db.add(source)
    db.flush()
    index_pages(db, embedder, source, [Page(None, text)])
    return source
