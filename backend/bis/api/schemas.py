"""Схемы API реестров."""

from __future__ import annotations

import datetime as dt
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def partial(model: type[BaseModel]) -> type[BaseModel]:
    """Схема частичного обновления: все поля необязательны."""
    fields = {name: (f.annotation | None, None) for name, f in model.model_fields.items()}
    return create_model(f"{model.__name__}Update", **fields)  # type: ignore[call-overload]


def read_model(model: type[BaseModel]) -> type[BaseModel]:
    return create_model(  # type: ignore[call-overload]
        f"{model.__name__.removesuffix('In')}Out",
        __base__=(model, ORM),
        id=(uuid.UUID, ...),
        created_at=(datetime | None, None),
    )


class CounterpartyIn(BaseModel):
    name: str
    kind: str = Field("other", description="customer, developer, designer, subcontractor, supplier, other")
    inn: str | None = None
    kpp: str | None = None
    ogrn: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    sro: str | None = None
    head_position: str | None = None
    head_name: str | None = None


class ObjectIn(BaseModel):
    name: str
    code: str | None = None
    address: str | None = None
    status: str = "active"
    developer_id: uuid.UUID | None = None
    designer_id: uuid.UUID | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ContractIn(BaseModel):
    kind: str = Field(description="general, subcontract, supply, services, design")
    number: str
    date: dt.date | None = None
    subject: str | None = None
    object_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    our_role: str = "contractor"
    amount: Decimal | None = None
    vat_rate: Decimal | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    status: str = "active"
    responsible_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class PersonIn(BaseModel):
    full_name: str
    position: str | None = None
    counterparty_id: uuid.UUID | None = None
    nrs_id: str | None = None
    phone: str | None = None
    email: str | None = None


class AppointmentIn(BaseModel):
    person_id: uuid.UUID
    object_id: uuid.UUID
    role: str = Field(description="developer_control, builder, builder_control, designer, performer или своя роль")
    order_number: str | None = None
    order_date: dt.date | None = None
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None


class ObligationIn(BaseModel):
    contract_id: uuid.UUID
    description: str
    party: str = "we"
    kind: str = "other"
    due_date: dt.date | None = None
    due_text: str | None = None
    amount: Decimal | None = None
    responsible_id: uuid.UUID | None = None
    status: str = "open"
    penalty: str | None = None
    source_clause: str | None = None
    notify_days_before: int = 3


class TaskIn(BaseModel):
    title: str
    description: str | None = None
    assignee_person_id: uuid.UUID | None = None
    assignee_user_id: uuid.UUID | None = None
    due_date: dt.date | None = None
    status: str = "open"
    priority: str = "normal"
    object_id: uuid.UUID | None = None
    contract_id: uuid.UUID | None = None
    result: str | None = None


class DocumentOut(ORM):
    id: uuid.UUID
    kind: str
    title: str
    number: str | None
    date: dt.date | None
    status: str
    source: str
    object_id: uuid.UUID | None
    contract_id: uuid.UUID | None
    file_name: str | None
    issues: list[dict[str, Any]]
    created_by: str | None
    created_at: datetime
    approved_at: datetime | None
    reject_reason: str | None
    data: dict[str, Any] = Field(default_factory=dict)


class UserIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    full_name: str
    position: str | None = None
    role: str = "viewer"
    password: str = Field(min_length=8)


class UserOut(ORM):
    id: uuid.UUID
    email: str
    full_name: str
    position: str | None
    role: str
    is_active: bool
    is_platform_admin: bool
    organization_id: uuid.UUID


class OrganizationOut(ORM):
    id: uuid.UUID
    name: str
    short_name: str | None
    inn: str | None
    kpp: str | None
    ogrn: str | None
    address: str | None
    phone: str | None
    email: str | None
    sro: str | None
    settings: dict[str, Any]


class OrganizationUpdate(BaseModel):
    name: str | None = None
    short_name: str | None = None
    inn: str | None = None
    kpp: str | None = None
    ogrn: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    sro: str | None = None
    settings: dict[str, Any] | None = None


class MemoryIn(BaseModel):
    kind: str = "rule"
    scope: str = "*"
    content: str
    is_active: bool = True


class MemoryOut(ORM):
    object_id: uuid.UUID | None = None
    id: uuid.UUID
    kind: str
    scope: str
    content: str
    source: str | None
    is_active: bool
    created_at: datetime


class EstimateItemIn(BaseModel):
    contract_id: uuid.UUID
    object_id: uuid.UUID | None = None
    ord: int = 0
    section: str | None = None
    code: str | None = None
    name: str
    unit: str
    quantity: Decimal
    unit_price: Decimal | None = None
    requires_aosr: bool = False


class ScheduleTaskIn(BaseModel):
    object_id: uuid.UUID
    estimate_item_id: uuid.UUID | None = None
    name: str
    planned_start: dt.date
    planned_end: dt.date
    planned_quantity: Decimal | None = None
    unit: str | None = None
    responsible_id: uuid.UUID | None = None
    ord: int = 0


class WorkVolumeIn(BaseModel):
    object_id: uuid.UUID
    estimate_item_id: uuid.UUID
    date: dt.date
    quantity: Decimal
    location: str | None = None
    status: str = "reported"
    aosr_document_id: uuid.UUID | None = None
    note: str | None = None


class MaterialIn(BaseModel):
    name: str
    unit: str
    code: str | None = None
    category: str | None = None


class MaterialNormIn(BaseModel):
    estimate_item_id: uuid.UUID
    material_id: uuid.UUID
    rate: Decimal
    loss_percent: Decimal = Decimal(0)
    source: str | None = None


class MaterialReceiptIn(BaseModel):
    object_id: uuid.UUID
    material_id: uuid.UUID
    date: dt.date
    quantity: Decimal
    price: Decimal | None = None
    supplier_id: uuid.UUID | None = None
    contract_id: uuid.UUID | None = None
    document_ref: str | None = None
    quality_doc: str | None = None
    is_customer_supplied: bool = False


class MaterialUsageIn(BaseModel):
    object_id: uuid.UUID
    material_id: uuid.UUID
    estimate_item_id: uuid.UUID | None = None
    date: dt.date
    quantity: Decimal
    note: str | None = None
