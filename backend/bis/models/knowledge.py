"""База знаний: источники (нормативы, документы организации) и фрагменты для RAG."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, Date, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, REAL, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from bis.config import get_settings
from bis.db import Base, IdMixin

EMBEDDING_DIM = get_settings().embedding_dim
VECTOR = get_settings().vector_enabled


class KnowledgeSource(IdMixin, Base):
    """Источник знаний. organization_id = NULL — общая нормативная библиотека платформы."""

    __tablename__ = "knowledge_sources"

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[str] = mapped_column(String(50), index=True)  # norm | org_document | letter | contract | project_doc
    title: Mapped[str] = mapped_column(String(1000))
    designation: Mapped[str | None] = mapped_column(String(200), index=True)  # «СП 70.13330.2012»
    status: Mapped[str] = mapped_column(String(30), default="active")  # active | cancelled | replaced | unknown
    effective_date: Mapped[date | None] = mapped_column(Date)
    file_key: Mapped[str | None] = mapped_column(String(1000))
    file_name: Mapped[str | None] = mapped_column(String(500))
    mime: Mapped[str | None] = mapped_column(String(200))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class KnowledgeChunk(IdMixin, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        Index("ix_knowledge_chunks_tsv", "tsv", postgresql_using="gin"),
        *([Index("ix_knowledge_chunks_embedding", "embedding", postgresql_using="hnsw",
                 postgresql_ops={"embedding": "vector_cosine_ops"})] if VECTOR else []),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_sources.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    ord: Mapped[int] = mapped_column(Integer)
    clause: Mapped[str | None] = mapped_column(String(50))  # номер пункта: «5.16.3», «А.2»
    section_path: Mapped[str | None] = mapped_column(Text)
    heading: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM) if VECTOR else ARRAY(REAL))
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('russian', coalesce(section_path, '') || ' ' || coalesce(heading, '') || ' ' || content)",
            persisted=True,
        ),
    )
