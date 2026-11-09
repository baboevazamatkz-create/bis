"""Shared access to the complete indexed text, including OCR and visual descriptions."""

from __future__ import annotations

import contextlib

from sqlalchemy import select

from bis.models import KnowledgeChunk, KnowledgeSource
from bis.rag.search import _hit


def document_chunks(db, doc):
    rows = db.execute(select(KnowledgeChunk, KnowledgeSource).join(
        KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id).where(
        KnowledgeSource.organization_id == doc.organization_id, KnowledgeSource.document_id == doc.id)
        .order_by(KnowledgeSource.created_at, KnowledgeSource.id, KnowledgeChunk.ord)).all()
    # Text and vision are separate sources: never interleave their chunk ordinals.
    rows.sort(key=lambda row: bool((row[1].meta or {}).get("vision")))
    return [_hit(chunk, source) for chunk, source in rows]


def document_text(db, doc, storage=None):
    chunks = document_chunks(db, doc)
    text = "\n".join(c.content for c in chunks)
    if not text.strip() and storage is not None and doc.file_key:
        from bis.rag.parsers import UnsupportedFormat, parse_file

        with contextlib.suppress(UnsupportedFormat):
            text = parse_file(storage.get(doc.file_key), doc.file_name or "file", doc.mime).text
    return text
