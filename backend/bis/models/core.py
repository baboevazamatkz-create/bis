"""Ядро предметной области: организации, пользователи, объекты, контрагенты, договоры,
ответственные лица, реестр документов, аудит, память организации."""

from __future__ import annotations

import datetime as dt
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bis.db import Base, IdMixin, TenantMixin, TimestampMixin


class Organization(IdMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(500))
    short_name: Mapped[str | None] = mapped_column(String(200))
    inn: Mapped[str | None] = mapped_column(String(12))
    kpp: Mapped[str | None] = mapped_column(String(9))
    ogrn: Mapped[str | None] = mapped_column(String(15))
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(200))
    sro: Mapped[str | None] = mapped_column(Text)  # сведения о членстве в СРО
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class User(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(200), unique=True)
    full_name: Mapped[str] = mapped_column(String(300))
    position: Mapped[str | None] = mapped_column(String(300))
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(50), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(50))


class Counterparty(IdMixin, TimestampMixin, TenantMixin, Base):
    """Контрагент: заказчик, технический заказчик, проектировщик, субподрядчик, поставщик."""

    __tablename__ = "counterparties"

    name: Mapped[str] = mapped_column(String(500))
    kind: Mapped[str] = mapped_column(String(50), default="other")
    inn: Mapped[str | None] = mapped_column(String(12))
    kpp: Mapped[str | None] = mapped_column(String(9))
    ogrn: Mapped[str | None] = mapped_column(String(15))
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(200))
    sro: Mapped[str | None] = mapped_column(Text)
    head_position: Mapped[str | None] = mapped_column(String(300))
    head_name: Mapped[str | None] = mapped_column(String(300))


class ConstructionObject(IdMixin, TimestampMixin, TenantMixin, Base):
    """Объект капитального строительства."""

    __tablename__ = "construction_objects"

    name: Mapped[str] = mapped_column(String(1000))
    code: Mapped[str | None] = mapped_column(String(100))
    address: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="active")
    developer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("counterparties.id", ondelete="SET NULL")
    )  # застройщик / технический заказчик
    designer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("counterparties.id", ondelete="SET NULL")
    )  # лицо, осуществляющее подготовку ПД
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Contract(IdMixin, TimestampMixin, TenantMixin, Base):
    """Договор подряда, субподряда, поставки, услуг, проектирования."""

    __tablename__ = "contracts"

    kind: Mapped[str] = mapped_column(String(50))
    number: Mapped[str] = mapped_column(String(100))
    date: Mapped[dt.date | None] = mapped_column(Date)
    subject: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("construction_objects.id", ondelete="SET NULL"), index=True
    )
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("counterparties.id", ondelete="SET NULL"), index=True
    )
    our_role: Mapped[str] = mapped_column(String(50), default="contractor")  # contractor | customer
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    vat_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    start_date: Mapped[dt.date | None] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(50), default="active")
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )
    responsible_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id", ondelete="SET NULL")
    )  # ответственный за исполнение договора
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Person(IdMixin, TimestampMixin, TenantMixin, Base):
    """Ответственное лицо (сотрудник организации или представитель контрагента)."""

    __tablename__ = "persons"

    full_name: Mapped[str] = mapped_column(String(300))
    position: Mapped[str | None] = mapped_column(String(300))
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("counterparties.id", ondelete="SET NULL")
    )  # NULL — сотрудник нашей организации
    nrs_id: Mapped[str | None] = mapped_column(String(50))  # номер в Национальном реестре специалистов
    phone: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(200))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class Appointment(IdMixin, TimestampMixin, TenantMixin, Base):
    """Назначение ответственного на объект распорядительным документом (приказом).

    role — роль в ИД (см. bis.documents.aosr.REPRESENTATIVE_ROLES).
    """

    __tablename__ = "appointments"

    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id", ondelete="CASCADE"), index=True
    )
    object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("construction_objects.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(100))
    order_number: Mapped[str | None] = mapped_column(String(100))
    order_date: Mapped[dt.date | None] = mapped_column(Date)
    valid_from: Mapped[dt.date | None] = mapped_column(Date)
    valid_to: Mapped[dt.date | None] = mapped_column(Date)

    person: Mapped[Person] = relationship(lazy="joined")


class ContractObligation(IdMixin, TimestampMixin, TenantMixin, Base):
    """Обязательство по договору — основа для агентов контроля ответственности."""

    __tablename__ = "contract_obligations"

    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="CASCADE"), index=True
    )
    description: Mapped[str] = mapped_column(Text)
    party: Mapped[str] = mapped_column(String(20), default="we")  # we | counterparty
    due_date: Mapped[dt.date | None] = mapped_column(Date)
    responsible_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("persons.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(30), default="open")  # open | done | overdue | cancelled
    kind: Mapped[str] = mapped_column(String(30), default="other")  # works | payment | documents | warranty | insurance | other
    due_text: Mapped[str | None] = mapped_column(Text)  # срок словами: «в течение 10 рабочих дней после подписания КС-2»
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    penalty: Mapped[str | None] = mapped_column(Text)
    source_clause: Mapped[str | None] = mapped_column(String(100))
    source_text: Mapped[str | None] = mapped_column(Text)
    notify_days_before: Mapped[int] = mapped_column(Integer, default=3)
    extracted_by: Mapped[str | None] = mapped_column(String(100))  # пользователь или агент/модель


class Document(IdMixin, TimestampMixin, TenantMixin, Base):
    """Реестр документов: сгенерированные агентами, загруженные, полученные по почте."""

    __tablename__ = "documents"

    kind: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str] = mapped_column(String(1000))
    number: Mapped[str | None] = mapped_column(String(100))
    date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    source: Mapped[str] = mapped_column(String(30), default="generated")  # generated | uploaded | email
    object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("construction_objects.id", ondelete="SET NULL"), index=True
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="SET NULL", use_alter=True)
    )
    file_key: Mapped[str | None] = mapped_column(String(1000))
    file_name: Mapped[str | None] = mapped_column(String(500))
    mime: Mapped[str | None] = mapped_column(String(200))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    created_by: Mapped[str | None] = mapped_column(String(300))  # user:<id> | agent:<name>
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)


class DocumentTemplate(IdMixin, TimestampMixin, TenantMixin, Base):
    """Шаблон документа организации (DOCX с полями Jinja/docxtpl)."""

    __tablename__ = "document_templates"

    kind: Mapped[str] = mapped_column(String(50), index=True)
    name: Mapped[str] = mapped_column(String(300))
    file_key: Mapped[str] = mapped_column(String(1000))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class Counter(Base):
    """Счётчики нумерации документов (исходящие письма, акты) по организации."""

    __tablename__ = "counters"
    __table_args__ = (UniqueConstraint("organization_id", "key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(String(200))
    value: Mapped[int] = mapped_column(Integer, default=0)


class AuditLog(IdMixin, TenantMixin, Base):
    """Журнал действий пользователей и агентов."""

    __tablename__ = "audit_log"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    actor_type: Mapped[str] = mapped_column(String(20))  # user | agent | system
    actor: Mapped[str] = mapped_column(String(300))
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str | None] = mapped_column(String(100))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class OrgMemory(IdMixin, TimestampMixin, TenantMixin, Base):
    """Память организации: правила, стиль, факты, примеры из правок пользователей.

    Агенты добавляют активные записи в системный промпт — так система подстраивается под компанию.
    """

    __tablename__ = "org_memory"

    object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("construction_objects.id", ondelete="CASCADE"), index=True)

    kind: Mapped[str] = mapped_column(String(30), default="rule")  # rule | style | fact | example
    scope: Mapped[str] = mapped_column(String(50), default="*")  # имя агента или *
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ChatSession(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "chat_sessions"

    object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("construction_objects.id", ondelete="CASCADE"), index=True)

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(500), default="Новый диалог")


class ChatMessage(IdMixin, Base):
    __tablename__ = "chat_messages"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    role: Mapped[str] = mapped_column(String(20))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    agent: Mapped[str | None] = mapped_column(String(100))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
