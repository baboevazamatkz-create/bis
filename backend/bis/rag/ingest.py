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


def chunk_head(source: KnowledgeSource) -> str:
    """Шапка фрагмента для смыслового поиска: обозначение норматива или карточка документа (вид, №, дата, о чём)."""
    return source.designation or ((source.meta or {}).get("context")) or source.title


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
    from bis.services.project_facts import invalidate_document

    invalidate_document(db, source.document_id)
    db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.source_id == source.id))
    if source.kind in NORMATIVE_KINDS:
        chunks = NormativeChunker().chunk(pages, source.designation)
    else:
        chunks = GenericChunker().chunk(pages)
    texts = [embedding_text(chunk_head(source), c.section_path, c.heading, c.content) for c in chunks]
    from bis.models.knowledge import VECTOR

    # без pgvector векторы не нужны: поиск полнотекстовый и по номерам пунктов
    keep = VECTOR or getattr(embedder, "semantic", False)  # смысловые векторы храним и без pgvector
    vectors = [None] * len(texts)
    if texts and keep:
        try:
            vectors = embedder.embed_documents(texts)
            if len(vectors) != len(texts):
                raise ValueError('Провайдер вернул неполный набор векторов')
            if VECTOR:
                from bis.models.knowledge import EMBEDDING_DIM

                if any(len(v) != EMBEDDING_DIM for v in vectors):
                    raise ValueError('Размерность векторов не совпадает с индексом')
            source.meta = {**(source.meta or {}), 'embedding_status': 'ready'}
        except Exception as exc:  # noqa: BLE001 — preserve text and citations when an optional AI service fails
            vectors = [None] * len(texts)
            source.meta = {**(source.meta or {}), 'embedding_status': 'error',
                           'embedding_error': f'{type(exc).__name__}: {str(exc)[:200]}'}
    else:
        source.meta = {**(source.meta or {}), 'embedding_status': 'lexical'}
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


def ingest_pages(
    db: Session,
    embedder: Embedder,
    *,
    org_id: uuid.UUID | None,
    pages: list[Page],
    kind: str,
    title: str,
    document_id: uuid.UUID | None = None,
    context: str | None = None,
) -> KnowledgeSource:
    """Индексация текста по страницам: ссылка в ответе ведёт на страницу. context — шапка карточки документа
    («АОСР № 1 от 12.04.2026 · армирование плиты Фм1»): добавляется к каждому фрагменту для смыслового поиска."""
    source = KnowledgeSource(organization_id=org_id, kind=kind, title=title, designation=None, document_id=document_id,
                             meta={"context": context} if context else {})
    db.add(source)
    db.flush()
    index_pages(db, embedder, source, pages)
    return source
