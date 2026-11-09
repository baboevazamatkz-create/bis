"""Versioned project assertions, their contradictions and reproducible investigations."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from bis.db import Base, IdMixin, TenantMixin, TimestampMixin


class FactSnapshot(IdMixin, TenantMixin, Base):
    __tablename__ = 'fact_snapshots'
    __table_args__ = (
        UniqueConstraint('organization_id', 'object_id', 'origin_key', 'fingerprint'),
        CheckConstraint('num_nonnulls(document_id, intake_event_id, work_volume_id) = 1', name='fact_one_origin'),
        Index('ix_fact_snapshot_project_state', 'organization_id', 'object_id', 'state'),
    )
    object_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey('construction_objects.id', ondelete='CASCADE'), index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey('documents.id', ondelete='CASCADE'), index=True)
    intake_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey('intake_events.id', ondelete='CASCADE'), index=True)
    work_volume_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey('work_volumes.id', ondelete='CASCADE'), index=True)
    library_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey('library_versions.id', ondelete='SET NULL'))
    origin_key: Mapped[str] = mapped_column(String(150))
    fingerprint: Mapped[str] = mapped_column(String(64))
    text_sha256: Mapped[str] = mapped_column(String(64))
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    file_key: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(50))
    state: Mapped[str] = mapped_column(String(30))  # active | partial | unread | stale | superseded
    method: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectFact(IdMixin, TenantMixin, Base):
    __tablename__ = 'project_facts'
    __table_args__ = (Index('ix_project_fact_lookup', 'organization_id', 'object_id', 'predicate', 'location_key', 'work_key'),)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey('construction_objects.id', ondelete='CASCADE'), index=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey('fact_snapshots.id', ondelete='CASCADE'), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    predicate: Mapped[str] = mapped_column(String(50))
    category: Mapped[str] = mapped_column(String(30))  # planned | actual | reference | requirement | result
    value: Mapped[str] = mapped_column(Text)
    normalized_value: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    location_key: Mapped[str] = mapped_column(String(500), default='')
    work_key: Mapped[str] = mapped_column(String(500), default='')
    entity_key: Mapped[str] = mapped_column(String(500), default='')
    quote: Mapped[str] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey('knowledge_chunks.id', ondelete='SET NULL'))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FactConflict(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = 'fact_conflicts'
    __table_args__ = (UniqueConstraint('organization_id', 'object_id', 'group_key'),)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey('construction_objects.id', ondelete='CASCADE'), index=True)
    group_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(40))
    predicate: Mapped[str] = mapped_column(String(50))
    location_key: Mapped[str] = mapped_column(String(500))
    work_key: Mapped[str] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(30))  # open | resolved | stale
    revision: Mapped[int] = mapped_column(Integer, default=1)
    signature: Mapped[str] = mapped_column(String(64))
    fact_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    message: Mapped[str] = mapped_column(Text)


class FactDecision(IdMixin, TenantMixin, Base):
    __tablename__ = 'fact_decisions'
    __table_args__ = (UniqueConstraint('conflict_id', 'revision'),)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey('construction_objects.id', ondelete='CASCADE'), index=True)
    conflict_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey('fact_conflicts.id', ondelete='CASCADE'), index=True)
    fact_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey('project_facts.id', ondelete='SET NULL'))
    revision: Mapped[int] = mapped_column(Integer)
    signature: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey('users.id', ondelete='RESTRICT'))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC))


class BrainInvestigation(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = 'brain_investigations'
    object_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey('construction_objects.id', ondelete='CASCADE'), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey('users.id', ondelete='SET NULL'))
    question: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(30))
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
