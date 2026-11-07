"""Учёт работ и материалов: смета (ведомость объёмов), ГПР, выполненные объёмы, материалы, нормы, приход, расход."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from bis.db import Base, IdMixin, TenantMixin, TimestampMixin


def _fk(table: str, ondelete: str = "SET NULL", index: bool = False):
    return mapped_column(UUID(as_uuid=True), ForeignKey(f"{table}.id", ondelete=ondelete), index=index)


class EstimateItem(IdMixin, TimestampMixin, TenantMixin, Base):
    """Позиция сметы / ведомости договорной цены."""

    __tablename__ = "estimate_items"

    contract_id: Mapped[uuid.UUID] = _fk("contracts", "CASCADE", index=True)
    object_id: Mapped[uuid.UUID | None] = _fk("construction_objects", index=True)
    ord: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[str | None] = mapped_column(String(500))
    code: Mapped[str | None] = mapped_column(String(100))  # № позиции, шифр расценки (ГЭСН/ФЕР)
    name: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(String(50))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))  # без НДС
    requires_aosr: Mapped[bool] = mapped_column(Boolean, default=False)  # скрытые работы: закрывать только с АОСР


class ScheduleTask(IdMixin, TimestampMixin, TenantMixin, Base):
    """Работа графика производства работ (ГПР)."""

    __tablename__ = "schedule_tasks"

    object_id: Mapped[uuid.UUID] = _fk("construction_objects", "CASCADE", index=True)
    estimate_item_id: Mapped[uuid.UUID | None] = _fk("estimate_items")
    name: Mapped[str] = mapped_column(Text)
    planned_start: Mapped[dt.date] = mapped_column(Date)
    planned_end: Mapped[dt.date] = mapped_column(Date)
    planned_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    unit: Mapped[str | None] = mapped_column(String(50))
    responsible_id: Mapped[uuid.UUID | None] = _fk("persons")
    ord: Mapped[int] = mapped_column(Integer, default=0)


class WorkVolume(IdMixin, TimestampMixin, TenantMixin, Base):
    """Выполненный объём работ (факт): reported → confirmed → closed (вошёл в утверждённый КС-2)."""

    __tablename__ = "work_volumes"

    object_id: Mapped[uuid.UUID] = _fk("construction_objects", "CASCADE", index=True)
    estimate_item_id: Mapped[uuid.UUID] = _fk("estimate_items", "CASCADE", index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    location: Mapped[str | None] = mapped_column(String(500))  # оси, захватка, этаж
    status: Mapped[str] = mapped_column(String(20), default="reported", index=True)
    reported_by: Mapped[str | None] = mapped_column(String(300))
    aosr_document_id: Mapped[uuid.UUID | None] = _fk("documents")
    ks2_document_id: Mapped[uuid.UUID | None] = _fk("documents")
    note: Mapped[str | None] = mapped_column(Text)


class Material(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "materials"

    name: Mapped[str] = mapped_column(String(500))
    unit: Mapped[str] = mapped_column(String(50))
    code: Mapped[str | None] = mapped_column(String(100))  # код номенклатуры (1С)
    category: Mapped[str | None] = mapped_column(String(200))


class MaterialNorm(IdMixin, TimestampMixin, TenantMixin, Base):
    """Норма расхода материала на единицу работы позиции сметы."""

    __tablename__ = "material_norms"

    estimate_item_id: Mapped[uuid.UUID] = _fk("estimate_items", "CASCADE", index=True)
    material_id: Mapped[uuid.UUID] = _fk("materials", "CASCADE", index=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    loss_percent: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=0)  # нормативные потери
    source: Mapped[str | None] = mapped_column(String(300))  # ГЭСН, ППР, норма компании


class MaterialReceipt(IdMixin, TimestampMixin, TenantMixin, Base):
    """Поступление материала на объект (УПД, накладная, давальческий материал)."""

    __tablename__ = "material_receipts"

    object_id: Mapped[uuid.UUID] = _fk("construction_objects", "CASCADE", index=True)
    material_id: Mapped[uuid.UUID] = _fk("materials", "CASCADE", index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))  # за единицу без НДС
    supplier_id: Mapped[uuid.UUID | None] = _fk("counterparties")
    contract_id: Mapped[uuid.UUID | None] = _fk("contracts")
    document_ref: Mapped[str | None] = mapped_column(String(300))  # УПД № 15 от 01.06.2026
    quality_doc: Mapped[str | None] = mapped_column(String(500))  # сертификат / паспорт качества
    is_customer_supplied: Mapped[bool] = mapped_column(Boolean, default=False)  # давальческий


class MaterialUsage(IdMixin, TimestampMixin, TenantMixin, Base):
    """Фактический отпуск материала в работу (по данным прораба/склада)."""

    __tablename__ = "material_usage"

    object_id: Mapped[uuid.UUID] = _fk("construction_objects", "CASCADE", index=True)
    material_id: Mapped[uuid.UUID] = _fk("materials", "CASCADE", index=True)
    estimate_item_id: Mapped[uuid.UUID | None] = _fk("estimate_items")
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    note: Mapped[str | None] = mapped_column(Text)
