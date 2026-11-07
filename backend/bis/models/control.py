"""Контроль исполнения: поручения, уведомления, журнал фоновых задач."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from bis.db import Base, IdMixin, TenantMixin, TimestampMixin


class Task(IdMixin, TimestampMixin, TenantMixin, Base):
    """Поручение: из письма, договора, совещания, от агента или пользователя."""

    __tablename__ = "tasks"

    title: Mapped[str] = mapped_column(String(1000))
    description: Mapped[str | None] = mapped_column(Text)
    assignee_person_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id", ondelete="SET NULL"), index=True
    )
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    due_date: Mapped[dt.date | None] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)  # open | in_progress | done | cancelled
    priority: Mapped[str] = mapped_column(String(20), default="normal")  # low | normal | high
    source_type: Mapped[str | None] = mapped_column(String(50))  # letter | obligation | agent | user | meeting
    source_id: Mapped[str | None] = mapped_column(String(100))
    object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("construction_objects.id", ondelete="SET NULL")
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="SET NULL"))
    created_by: Mapped[str | None] = mapped_column(String(300))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(Text)


class Notification(IdMixin, TenantMixin, Base):
    """Уведомление пользователю. key — для идемпотентности фоновых проверок (одно напоминание на событие)."""

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("organization_id", "key"),)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                                                      index=True)
    level: Mapped[str] = mapped_column(String(20), default="info")  # info | warning | critical
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(String(50))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    key: Mapped[str | None] = mapped_column(String(300))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    channels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # статусы доставки: telegram, email


class JobRun(IdMixin, Base):
    __tablename__ = "job_runs"

    name: Mapped[str] = mapped_column(String(100), index=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ok | error
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
