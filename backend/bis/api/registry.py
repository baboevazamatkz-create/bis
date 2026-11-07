"""CRUD реестров организации с изоляцией по тенанту."""


import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from bis.api.deps import require
from bis.api.schemas import (
    AppointmentIn,
    ContractIn,
    CounterpartyIn,
    EstimateItemIn,
    MaterialIn,
    MaterialNormIn,
    MaterialReceiptIn,
    MaterialUsageIn,
    ObjectIn,
    ObligationIn,
    PersonIn,
    ScheduleTaskIn,
    TaskIn,
    WorkVolumeIn,
    partial,
    read_model,
)
from bis.db import get_db
from bis.models import (
    Appointment,
    ConstructionObject,
    Contract,
    ContractObligation,
    Counterparty,
    Document,
    EstimateItem,
    Material,
    MaterialNorm,
    MaterialReceipt,
    MaterialUsage,
    Person,
    ScheduleTask,
    Task,
    User,
    WorkVolume,
)
from bis.services.audit import audit

FK_MODELS: dict[str, Any] = {
    "developer_id": Counterparty,
    "designer_id": Counterparty,
    "counterparty_id": Counterparty,
    "object_id": ConstructionObject,
    "person_id": Person,
    "contract_id": Contract,
    "responsible_id": Person,
    "assignee_person_id": Person,
    "assignee_user_id": User,
    "estimate_item_id": EstimateItem,
    "material_id": Material,
    "supplier_id": Counterparty,
    "aosr_document_id": Document,
    "document_id": Document,
}


def _check_fks(db: Session, org_id: uuid.UUID, values: dict[str, Any]) -> None:
    for field, value in values.items():
        model = FK_MODELS.get(field)
        if model is None or value is None:
            continue
        row = db.get(model, value)
        if row is None or row.organization_id != org_id:
            raise HTTPException(400, f"Связанная запись не найдена: {field}={value}")


def crud_router(prefix: str, model: Any, schema: type[BaseModel], search: tuple[str, ...], entity: str,
                filters: tuple[str, ...] = ()) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=[entity])
    out_schema = read_model(schema)
    upd_schema = partial(schema)

    def get_owned(db: Session, user: User, item_id: uuid.UUID):
        row = db.get(model, item_id)
        if row is None or row.organization_id != user.organization_id:
            raise HTTPException(404, "Запись не найдена")
        return row

    @router.get("", response_model=list[out_schema])
    def list_items(q: str | None = None, limit: int = 100, offset: int = 0, filter_field: str | None = None,
                   filter_value: str | None = None, db: Session = Depends(get_db),
                   user: User = Depends(require("registry:read"))):
        stmt = select(model).where(model.organization_id == user.organization_id)
        if q and search:
            stmt = stmt.where(or_(*(getattr(model, f).ilike(f"%{q}%") for f in search)))
        if filter_field and filter_field in filters and filter_value:
            stmt = stmt.where(getattr(model, filter_field) == filter_value)
        return db.scalars(stmt.order_by(model.created_at.desc()).limit(min(limit, 500)).offset(offset)).all()

    @router.post("", response_model=out_schema, status_code=201)
    def create_item(body: schema, db: Session = Depends(get_db),  # type: ignore[valid-type]
                    user: User = Depends(require("registry:write"))):
        values = body.model_dump()
        _check_fks(db, user.organization_id, values)
        row = model(organization_id=user.organization_id, **values)
        db.add(row)
        db.flush()
        audit(db, user.organization_id, actor_type="user", actor=str(user.id), action=f"{entity}.create",
              entity_type=entity, entity_id=row.id)
        if entity == "object":  # первый объект: загруженный раньше архив относится к нему
            from bis.services.archive import assign_single_object

            assign_single_object(db, user.organization_id)
        db.commit()
        return row

    @router.get("/{item_id}", response_model=out_schema)
    def get_item(item_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("registry:read"))):
        return get_owned(db, user, item_id)

    @router.patch("/{item_id}", response_model=out_schema)
    def update_item(item_id: uuid.UUID, body: upd_schema, db: Session = Depends(get_db),  # type: ignore[valid-type]
                    user: User = Depends(require("registry:write"))):
        row = get_owned(db, user, item_id)
        values = body.model_dump(exclude_unset=True)
        _check_fks(db, user.organization_id, values)
        for k, v in values.items():
            setattr(row, k, v)
        audit(db, user.organization_id, actor_type="user", actor=str(user.id), action=f"{entity}.update",
              entity_type=entity, entity_id=row.id, details={"fields": list(values)})
        db.commit()
        db.refresh(row)
        return row

    @router.delete("/{item_id}", status_code=204)
    def delete_item(item_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("registry:write"))):
        row = get_owned(db, user, item_id)
        db.delete(row)
        audit(db, user.organization_id, actor_type="user", actor=str(user.id), action=f"{entity}.delete",
              entity_type=entity, entity_id=item_id)
        db.commit()

    return router


routers = [
    crud_router("/objects", ConstructionObject, ObjectIn, ("name", "code", "address"), "object"),
    crud_router("/counterparties", Counterparty, CounterpartyIn, ("name", "inn"), "counterparty", filters=("kind",)),
    crud_router("/contracts", Contract, ContractIn, ("number", "subject"), "contract", filters=("object_id", "kind")),
    crud_router("/persons", Person, PersonIn, ("full_name", "position"), "person", filters=("counterparty_id",)),
    crud_router("/appointments", Appointment, AppointmentIn, ("role", "order_number"), "appointment",
                filters=("object_id", "person_id")),
    crud_router("/obligations", ContractObligation, ObligationIn, ("description",), "obligation",
                filters=("contract_id", "status", "party")),
    crud_router("/tasks", Task, TaskIn, ("title", "description"), "task",
                filters=("status", "assignee_person_id", "assignee_user_id", "object_id")),
    crud_router("/estimate-items", EstimateItem, EstimateItemIn, ("name", "code", "section"), "estimate_item",
                filters=("contract_id", "object_id")),
    crud_router("/schedule", ScheduleTask, ScheduleTaskIn, ("name",), "schedule_task", filters=("object_id",)),
    crud_router("/volumes", WorkVolume, WorkVolumeIn, ("location", "note"), "work_volume",
                filters=("object_id", "estimate_item_id", "status")),
    crud_router("/materials", Material, MaterialIn, ("name", "code"), "material"),
    crud_router("/material-norms", MaterialNorm, MaterialNormIn, (), "material_norm",
                filters=("estimate_item_id", "material_id")),
    crud_router("/material-receipts", MaterialReceipt, MaterialReceiptIn, ("document_ref",), "material_receipt",
                filters=("object_id", "material_id")),
    crud_router("/material-usage", MaterialUsage, MaterialUsageIn, ("note",), "material_usage",
                filters=("object_id", "material_id")),
]
