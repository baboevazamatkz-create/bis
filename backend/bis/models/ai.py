"""ИИ-конвейер: входящие события с производства и действия ИИ-специалистов."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from bis.db import Base, IdMixin, TenantMixin, TimestampMixin


class IntakeEvent(IdMixin, TenantMixin, Base):
    """Сигнал с производства: фото, голосовое, документ или текст (Telegram, почта, загрузка, API)."""

    __tablename__ = "intake_events"

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    source: Mapped[str] = mapped_column(String(30), default="upload")  # upload | telegram | email | api
    kind: Mapped[str] = mapped_column(String(20))  # photo | voice | document | text
    text: Mapped[str | None] = mapped_column(Text)  # подпись, текст сообщения или расшифровка голосового
    file_key: Mapped[str | None] = mapped_column(String(1000))
    file_name: Mapped[str | None] = mapped_column(String(500))
    mime: Mapped[str | None] = mapped_column(String(200))
    object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("construction_objects.id", ondelete="SET NULL"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    sender: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)  # new | done | error
    detected_type: Mapped[str | None] = mapped_column(String(50))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"))


class AIAction(IdMixin, TimestampMixin, TenantMixin, Base):
    """Действие ИИ-специалиста: выполнено автоматически или ждёт решения человека."""

    __tablename__ = "ai_actions"

    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intake_events.id", ondelete="CASCADE"), index=True)
    specialist: Mapped[str] = mapped_column(String(50), index=True)
    kind: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    severity: Mapped[str] = mapped_column(String(20), default="info")  # info | warning | critical
    status: Mapped[str] = mapped_column(String(20), default="proposed", index=True)  # proposed | applied | rejected | failed
    auto: Mapped[bool] = mapped_column(Boolean, default=False)  # выполнено без человека
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)  # человек исправил перед утверждением
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("construction_objects.id", ondelete="SET NULL"))
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
