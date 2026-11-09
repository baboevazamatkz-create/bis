"""Versioned, incremental readings of project sheets; source files remain immutable."""
import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from bis.db import Base, IdMixin, TenantMixin, TimestampMixin


class ProjectReading(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = 'project_readings'
    __table_args__ = (UniqueConstraint('document_id', 'input_signature'),)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey('construction_objects.id', ondelete='CASCADE'), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey('documents.id', ondelete='CASCADE'), index=True)
    input_signature: Mapped[str] = mapped_column(String(64))
    file_key: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(Text)
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    result_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
