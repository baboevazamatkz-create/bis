"""Рейтинг рисков объектов (как Construction IQ у Autodesk и Newmetrix у Oracle): где горит и почему.

Балл объекта — сумма весов фактов из базы: просрочки, свежие замечания и простои, скрытые работы без АОСР,
неподтверждённые объёмы, материалы без документов о качестве. Без модели: каждый балл объясним и проверяем.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from bis.models import (
    AIAction,
    ConstructionObject,
    Contract,
    ContractObligation,
    EstimateItem,
    MaterialReceipt,
    Task,
    WorkVolume,
)

# (вес за случай, предел случаев) — чтобы один тип проблем не заслонял остальные
WEIGHTS = {
    "tasks_overdue": (3, 5),
    "obligations_overdue": (4, 5),
    "issues_week": (2, 6),
    "critical_week": (4, 3),
    "hidden_without_aosr": (2, 6),
    "unconfirmed_volumes": (1, 6),
    "no_quality_doc": (2, 5),
}
LABELS = {
    "tasks_overdue": "просрочено поручений",
    "obligations_overdue": "просрочено обязательств по договорам",
    "issues_week": "замечаний и простоев за неделю",
    "critical_week": "критических замечаний за неделю",
    "hidden_without_aosr": "скрытых работ без АОСР",
    "unconfirmed_volumes": "объёмов не подтверждено ПТО больше 3 дней",
    "no_quality_doc": "поставок без документа о качестве за месяц",
}


def _count(db: Session, stmt) -> int:
    return int(db.scalar(stmt) or 0)


def object_factors(db: Session, org_id: uuid.UUID, obj_id: uuid.UUID, today: dt.date) -> dict[str, int]:
    week_ago = dt.datetime.combine(today - dt.timedelta(days=7), dt.time.min).astimezone()
    cnt = select(func.count())
    return {
        "tasks_overdue": _count(db, cnt.select_from(Task).where(
            Task.organization_id == org_id, Task.object_id == obj_id, Task.status.in_(["open", "in_progress"]),
            Task.due_date < today)),
        "obligations_overdue": _count(db, cnt.select_from(ContractObligation).join(
            Contract, Contract.id == ContractObligation.contract_id).where(
            ContractObligation.organization_id == org_id, Contract.object_id == obj_id,
            ContractObligation.party == "we", ContractObligation.status.in_(["open", "overdue"]),
            ContractObligation.due_date < today)),
        "issues_week": _count(db, cnt.select_from(AIAction).where(
            AIAction.organization_id == org_id, AIAction.object_id == obj_id, AIAction.created_at >= week_ago,
            AIAction.status != "rejected",
            or_(and_(AIAction.kind == "flag_issue", AIAction.severity == "warning"),
                and_(AIAction.kind == "create_task", AIAction.title.like("Проблема на объекте:%"))))),
        "critical_week": _count(db, cnt.select_from(AIAction).where(
            AIAction.organization_id == org_id, AIAction.object_id == obj_id, AIAction.created_at >= week_ago,
            AIAction.status != "rejected", AIAction.kind == "flag_issue", AIAction.severity == "critical")),
        "hidden_without_aosr": _count(db, select(func.count(func.distinct(WorkVolume.estimate_item_id))).select_from(WorkVolume)
                                      .join(EstimateItem, EstimateItem.id == WorkVolume.estimate_item_id).where(
            WorkVolume.organization_id == org_id, WorkVolume.object_id == obj_id, EstimateItem.requires_aosr.is_(True),
            WorkVolume.aosr_document_id.is_(None), WorkVolume.date < today - dt.timedelta(days=2))),
        "unconfirmed_volumes": _count(db, cnt.select_from(WorkVolume).where(
            WorkVolume.organization_id == org_id, WorkVolume.object_id == obj_id, WorkVolume.status == "reported",
            WorkVolume.date < today - dt.timedelta(days=3))),
        "no_quality_doc": _count(db, cnt.select_from(MaterialReceipt).where(
            MaterialReceipt.organization_id == org_id, MaterialReceipt.object_id == obj_id,
            or_(MaterialReceipt.quality_doc.is_(None), MaterialReceipt.quality_doc == ""),
            MaterialReceipt.is_customer_supplied.is_(False), MaterialReceipt.date >= today - dt.timedelta(days=30))),
    }


def ranking(db: Session, org_id: uuid.UUID, today: dt.date | None = None) -> list[dict[str, Any]]:
    today = today or dt.date.today()
    out = []
    for obj in db.scalars(select(ConstructionObject).where(ConstructionObject.organization_id == org_id,
                                                           ConstructionObject.status == "active")):
        f = object_factors(db, org_id, obj.id, today)
        score = sum(WEIGHTS[k][0] * min(v, WEIGHTS[k][1]) for k, v in f.items())
        reasons = sorted(((WEIGHTS[k][0] * min(v, WEIGHTS[k][1]), f"{LABELS[k]}: {v}") for k, v in f.items() if v),
                         reverse=True)
        out.append({"object_id": str(obj.id), "object": obj.code or obj.name, "name": obj.name, "score": score,
                    "level": "high" if score >= 12 else "medium" if score >= 5 else "low",
                    "reasons": [r for _, r in reasons], "factors": f})
    return sorted(out, key=lambda x: -x["score"])
