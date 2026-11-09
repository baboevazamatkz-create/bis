"""Connected read-only folders and immutable, reviewable versions of their files."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from bis.db import Base, IdMixin, TimestampMixin


class LibraryConnection(IdMixin, TimestampMixin, Base):
    __tablename__ = "library_connections"
    __table_args__ = (CheckConstraint("(level = 'knowledge' AND organization_id IS NULL AND object_id IS NULL) OR "
        "(level = 'organization' AND organization_id IS NOT NULL AND object_id IS NULL) OR "
        "(level = 'project' AND organization_id IS NOT NULL AND object_id IS NOT NULL)", name="library_owner"),)
    level: Mapped[str] = mapped_column(String(20))
    organization_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    object_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("construction_objects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    path: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class LibraryFile(IdMixin, TimestampMixin, Base):
    __tablename__ = "library_files"
    __table_args__ = (UniqueConstraint("connection_id", "relative_path"),)
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("library_connections.id", ondelete="CASCADE"), index=True)
    relative_path: Mapped[str] = mapped_column(Text)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("library_versions.id", name="fk_library_active_version", use_alter=True, ondelete="SET NULL"), index=True)
    state: Mapped[str] = mapped_column(String(30), default="pending")
    error: Mapped[str | None] = mapped_column(Text)


class LibraryVersion(IdMixin, TimestampMixin, Base):
    __tablename__ = "library_versions"
    __table_args__ = (UniqueConstraint("file_id", "revision"),)
    file_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("library_files.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    file_key: Mapped[str] = mapped_column(Text)
    mime: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(30), default="ready")
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("knowledge_sources.id", ondelete="SET NULL"), index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("documents.id", ondelete="SET NULL"), index=True)
    form_kind: Mapped[str | None] = mapped_column(String(50))
    mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("users.id", ondelete="SET NULL"))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
