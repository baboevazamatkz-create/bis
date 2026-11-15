"""Подключение к БД и базовые классы моделей."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime
from functools import lru_cache

from sqlalchemy import DateTime, ForeignKey, create_engine, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from bis.config import get_settings


class Base(DeclarativeBase):
    pass


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TenantMixin:
    """Все данные организации привязаны к тенанту (organization_id)."""

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )


def get_engine(url: str | None = None) -> Engine:
    # Cache by the resolved URL, not by () versus (None,), which creates distinct pools.
    return _engine_for_url(url or get_settings().database_url)


@lru_cache
def _engine_for_url(url: str) -> Engine:
    settings = get_settings()
    connection_args = {'connect_timeout': 5, 'options':
        f'-c lock_timeout={settings.database_lock_timeout_ms} -c statement_timeout={settings.database_statement_timeout_ms}'}
    return create_engine(url, pool_pre_ping=True, future=True,
                         pool_timeout=settings.database_pool_timeout, connect_args=connection_args)


@lru_cache
def _session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


def SessionLocal() -> Session:  # noqa: N802 — привычное имя фабрики
    return _session_factory()()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
