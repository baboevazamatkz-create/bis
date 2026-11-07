"""Расчёты учёта работ и материалов: план-факт, расход по нормам (М-29), списание, закрытие (КС-2/КС-3).

Все числа считает код (Decimal), а не языковая модель. Функции compute_* — чистые, их легко проверять.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bis.jobs import job
from bis.models import (
    ConstructionObject,
    Contract,
    Document,
    EstimateItem,
    Material,
    MaterialNorm,
    MaterialReceipt,
    MaterialUsage,
    Organization,
    ScheduleTask,
    WorkVolume,
)
from bis.services.notify import managers, notify, users_for_person

D0 = Decimal("0")
CENT = Decimal("0.01")


def q2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def q4(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# --- поиск позиции сметы по тексту ---------------------------------------------------------------

_WORD = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)


def _stems(text: str) -> set[str]:
    return {w[:5] for w in _WORD.findall(text.lower().replace("ё", "е")) if len(w) > 2 or w.isdigit()}


def similarity(a: str, b: str) -> float:
    sa, sb = _stems(a), _stems(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb) * 0.5 + len(sa & sb) / len(sa) * 0.5


def find_items(db: Session, org_id: uuid.UUID, query: str, *, object_id: uuid.UUID | None = None,
               contract_id: uuid.UUID | None = None, limit: int = 5) -> list[tuple[EstimateItem, float]]:
    stmt = select(EstimateItem).where(EstimateItem.organization_id == org_id)
    if object_id:
        stmt = stmt.where(EstimateItem.object_id == object_id)
    if contract_id:
        stmt = stmt.where(EstimateItem.contract_id == contract_id)
    scored = [(it, similarity(query, f"{it.code or ''} {it.name} {it.section or ''}")) for it in db.scalars(stmt)]
    scored = [x for x in scored if x[1] > 0.05]
    return sorted(scored, key=lambda x: -x[1])[:limit]


HIDDEN_WORKS = re.compile(r"армирован|арматур|гидроизоляц|обратн\w* засыпк|основани|подготовк|закладн|антикорроз|"
                          r"уплотнени|теплоизоляц|грунтовк|прокладк\w* (?:кабел|труб)", re.IGNORECASE)


# --- план-факт ----------------------------------------------------------------------------------


@dataclass
class PlanFactRow:
    task_id: str
    name: str
    unit: str | None
    planned_start: dt.date
    planned_end: dt.date
    planned_quantity: Decimal
    planned_to_date: Decimal
    actual: Decimal
    percent_plan: float
    percent_fact: float
    deviation: Decimal
    status: str  # not_started | on_track | behind | ahead | overdue | done | no_data
    forecast_end: dt.date | None
    responsible_id: str | None = None


def compute_plan_fact_row(*, task_id: str, name: str, unit: str | None, start: dt.date, end: dt.date,
                          planned: Decimal, volumes: list[tuple[dt.date, Decimal]], on_date: dt.date,
                          tolerance: Decimal = Decimal("0.10"), responsible_id: str | None = None) -> PlanFactRow:
    total_days = (end - start).days + 1
    elapsed = min(max((on_date - start).days + 1, 0), total_days)
    planned_to_date = planned * Decimal(elapsed) / Decimal(total_days) if total_days > 0 else planned
    actual = sum((qv for d, qv in volumes if d <= on_date), D0)
    deviation = actual - planned_to_date
    if planned <= 0:
        status = "no_data"
    elif actual >= planned:
        status = "done"
    elif on_date > end:
        status = "overdue"
    elif on_date < start and actual == 0:
        status = "not_started"
    elif deviation < -planned * tolerance:
        status = "behind"
    elif deviation > planned * tolerance:
        status = "ahead"
    else:
        status = "on_track"
    forecast = None
    dated = sorted(d for d, qv in volumes if d <= on_date and qv > 0)
    if dated and actual < planned:
        days_worked = max((on_date - dated[0]).days + 1, 1)
        rate = actual / Decimal(days_worked)
        if rate > 0:
            forecast = on_date + dt.timedelta(days=math.ceil((planned - actual) / rate))
    elif actual >= planned and dated:
        forecast = dated[-1]

    def pct(x: Decimal) -> float:
        return float(round(x / planned * 100, 1)) if planned > 0 else 0.0

    return PlanFactRow(task_id=task_id, name=name, unit=unit, planned_start=start, planned_end=end,
                       planned_quantity=q4(planned), planned_to_date=q4(planned_to_date), actual=q4(actual),
                       percent_plan=pct(planned_to_date), percent_fact=pct(actual), deviation=q4(deviation),
                       status=status, forecast_end=forecast, responsible_id=responsible_id)


def plan_fact(db: Session, org_id: uuid.UUID, object_id: uuid.UUID, on_date: dt.date | None = None) -> dict[str, Any]:
    on_date = on_date or dt.date.today()
    tasks = db.scalars(select(ScheduleTask).where(ScheduleTask.organization_id == org_id,
                                                  ScheduleTask.object_id == object_id).order_by(ScheduleTask.ord,
                                                                                                ScheduleTask.planned_start))
    rows = []
    for t in tasks:
        item = db.get(EstimateItem, t.estimate_item_id) if t.estimate_item_id else None
        planned = t.planned_quantity if t.planned_quantity is not None else (item.quantity if item else D0)
        volumes = []
        if item:
            volumes = [(v.date, v.quantity) for v in db.scalars(select(WorkVolume).where(
                WorkVolume.estimate_item_id == item.id, WorkVolume.object_id == object_id))]
        rows.append(compute_plan_fact_row(task_id=str(t.id), name=t.name, unit=t.unit or (item.unit if item else None),
                                          start=t.planned_start, end=t.planned_end, planned=Decimal(planned),
                                          volumes=volumes, on_date=on_date,
                                          responsible_id=str(t.responsible_id) if t.responsible_id else None))
    summary: dict[str, int] = defaultdict(int)
    for r in rows:
        summary[r.status] += 1
    late = [r for r in rows if r.forecast_end and r.forecast_end > r.planned_end and r.status != "done"]
    return {"on_date": on_date, "rows": [asdict(r) for r in rows], "summary": dict(summary),
            "late_forecast": [r.name for r in late]}


@job("plan_fact", "План-факт по ГПР: отставания и прогноз сроков", every_minutes=24 * 60)
def plan_fact_job(db: Session, org: Organization, today: dt.date) -> dict[str, Any]:
    notified = 0
    bosses = managers(db, org.id)
    for obj in db.scalars(select(ConstructionObject).where(ConstructionObject.organization_id == org.id,
                                                           ConstructionObject.status == "active")):
        pf = plan_fact(db, org.id, obj.id, today)
        for r in pf["rows"]:
            if r["status"] not in ("behind", "overdue"):
                continue
            users = users_for_person(db, uuid.UUID(r["responsible_id"])) if r["responsible_id"] else []
            text = (f"{obj.code or obj.name}: «{r['name']}» — план на {today:%d.%m} {r['planned_to_date']} {r['unit'] or ''}, "
                    f"факт {r['actual']} ({r['percent_fact']}% из {r['planned_quantity']}). "
                    + (f"Прогноз окончания {r['forecast_end']:%d.%m.%Y} при плане {r['planned_end']:%d.%m.%Y}."
                       if r["forecast_end"] else "Работы не начаты."))
            notified += notify(db, org.id, users + bosses, title=f"Отставание от ГПР: {r['name'][:100]}", body=text,
                               level="critical" if r["status"] == "overdue" else "warning", entity_type="schedule_task",
                               entity_id=r["task_id"], key=f"pf:{r['task_id']}:{r['status']}:{today.isocalendar()[1]}")
    return {"notified": notified}


# --- материалы ----------------------------------------------------------------------------------


@dataclass
class M29Line:
    item_code: str | None
    item_name: str
    work_quantity: Decimal
    work_unit: str
    rate: Decimal
    loss_percent: Decimal
    norm_quantity: Decimal


@dataclass
class M29Row:
    material_id: str
    material: str
    unit: str
    lines: list[M29Line] = field(default_factory=list)
    norm_total: Decimal = D0
    actual: Decimal | None = None
    deviation: Decimal | None = None
    deviation_percent: float | None = None
    status: str = "no_actual"  # ok | economy | overspend | no_actual | no_norm
    avg_price: Decimal | None = None


def compute_m29(norm_lines: dict[str, tuple[str, str, list[M29Line]]], actual: dict[str, Decimal],
                materials: dict[str, tuple[str, str]], tolerance_pct: Decimal = Decimal("2")) -> list[M29Row]:
    """norm_lines: material_id → (название, ед., строки по работам); actual: material_id → факт."""
    rows: list[M29Row] = []
    ids = list(norm_lines) + [m for m in actual if m not in norm_lines]
    for mid in ids:
        name, unit = norm_lines[mid][:2] if mid in norm_lines else materials.get(mid, ("?", "?"))
        lines = norm_lines[mid][2] if mid in norm_lines else []
        row = M29Row(material_id=mid, material=name, unit=unit, lines=lines)
        row.norm_total = q4(sum((ln.norm_quantity for ln in lines), D0))
        fact = actual.get(mid)
        if fact is not None:
            row.actual = q4(fact)
            row.deviation = q4(fact - row.norm_total)
            if row.norm_total > 0:
                pct = row.deviation / row.norm_total * 100
                row.deviation_percent = float(round(pct, 2))
                row.status = "overspend" if pct > tolerance_pct else "economy" if pct < -tolerance_pct else "ok"
            else:
                row.status = "no_norm"
        rows.append(row)
    return rows


def _period_volumes(db: Session, org_id: uuid.UUID, object_id: uuid.UUID, start: dt.date, end: dt.date):
    stmt = (select(WorkVolume.estimate_item_id, func.sum(WorkVolume.quantity))
            .where(WorkVolume.organization_id == org_id, WorkVolume.object_id == object_id,
                   WorkVolume.date.between(start, end), WorkVolume.status.in_(["confirmed", "closed"]))
            .group_by(WorkVolume.estimate_item_id))
    return dict(db.execute(stmt).all())


def avg_price(db: Session, material_id: uuid.UUID, object_id: uuid.UUID, until: dt.date) -> Decimal | None:
    rows = db.execute(select(MaterialReceipt.quantity, MaterialReceipt.price).where(
        MaterialReceipt.material_id == material_id, MaterialReceipt.object_id == object_id,
        MaterialReceipt.date <= until, MaterialReceipt.price.is_not(None))).all()
    qty = sum((r[0] for r in rows), D0)
    return q2(sum((r[0] * r[1] for r in rows), D0) / qty) if qty > 0 else None


def m29(db: Session, org_id: uuid.UUID, object_id: uuid.UUID, start: dt.date, end: dt.date,
        tolerance_pct: Decimal = Decimal("2")) -> dict[str, Any]:
    volumes = _period_volumes(db, org_id, object_id, start, end)
    norm_lines: dict[str, tuple[str, str, list[M29Line]]] = {}
    materials: dict[str, tuple[str, str]] = {}
    for item_id, qty in volumes.items():
        item = db.get(EstimateItem, item_id)
        for norm in db.scalars(select(MaterialNorm).where(MaterialNorm.estimate_item_id == item_id)):
            mat = db.get(Material, norm.material_id)
            norm_qty = qty * norm.rate * (1 + norm.loss_percent / 100)
            entry = norm_lines.setdefault(str(mat.id), (mat.name, mat.unit, []))
            entry[2].append(M29Line(item.code, item.name, q4(qty), item.unit, norm.rate, norm.loss_percent, q4(norm_qty)))
    usage = dict(db.execute(
        select(MaterialUsage.material_id, func.sum(MaterialUsage.quantity))
        .where(MaterialUsage.organization_id == org_id, MaterialUsage.object_id == object_id,
               MaterialUsage.date.between(start, end)).group_by(MaterialUsage.material_id)).all())
    actual = {str(k): v for k, v in usage.items()}
    for mid in actual:
        if mid not in norm_lines:
            mat = db.get(Material, uuid.UUID(mid))
            materials[mid] = (mat.name, mat.unit)
    rows = compute_m29(norm_lines, actual, materials, tolerance_pct)
    for r in rows:
        r.avg_price = avg_price(db, uuid.UUID(r.material_id), object_id, end)
    issues = []
    if not volumes:
        issues.append({"level": "warning", "field": "volumes", "message": "Нет подтверждённых объёмов работ за период"})
    for r in rows:
        if r.status == "overspend":
            issues.append({"level": "warning", "field": r.material,
                           "message": f"Перерасход {r.material}: {r.deviation} {r.unit} ({r.deviation_percent}%) — нужно обоснование"})
        elif r.status == "no_actual":
            issues.append({"level": "warning", "field": r.material, "message": f"Нет данных о фактическом расходе: {r.material}"})
        elif r.status == "no_norm":
            issues.append({"level": "warning", "field": r.material, "message": f"Нет нормы расхода: {r.material}"})
    return {"rows": [asdict(r) for r in rows], "issues": issues, "period": [start, end]}


def write_off(m29_data: dict[str, Any], tolerance_pct: Decimal = Decimal("2")) -> dict[str, Any]:
    """Акт списания: факт в пределах нормы с допуском; перерасход сверх допуска не списывается без обоснования."""
    rows, total = [], D0
    for r in m29_data["rows"]:
        if r["actual"] is None or r["norm_total"] <= 0:
            continue
        limit = r["norm_total"] * (1 + tolerance_pct / 100)
        qty = min(r["actual"], limit)
        excess = r["actual"] - qty
        price = r["avg_price"]
        amount = q2(qty * price) if price is not None else None
        total += amount or D0
        rows.append({"material": r["material"], "unit": r["unit"], "norm": r["norm_total"], "actual": r["actual"],
                     "write_off": q4(qty), "excess": q4(excess), "price": price, "amount": amount})
    return {"rows": rows, "total": q2(total)}


def material_balance(db: Session, org_id: uuid.UUID, object_id: uuid.UUID, on_date: dt.date | None = None) -> list[dict]:
    on_date = on_date or dt.date.today()
    rec = dict(db.execute(select(MaterialReceipt.material_id, func.sum(MaterialReceipt.quantity)).where(
        MaterialReceipt.organization_id == org_id, MaterialReceipt.object_id == object_id,
        MaterialReceipt.date <= on_date).group_by(MaterialReceipt.material_id)).all())
    used = dict(db.execute(select(MaterialUsage.material_id, func.sum(MaterialUsage.quantity)).where(
        MaterialUsage.organization_id == org_id, MaterialUsage.object_id == object_id,
        MaterialUsage.date <= on_date).group_by(MaterialUsage.material_id)).all())
    out = []
    for mid in set(rec) | set(used):
        mat = db.get(Material, mid)
        r, u = rec.get(mid, D0), used.get(mid, D0)
        out.append({"material_id": str(mid), "material": mat.name, "unit": mat.unit, "received": q4(r), "used": q4(u),
                    "balance": q4(r - u), "problem": "Расход больше прихода — нет документов на поступление" if u > r else None})
    return sorted(out, key=lambda x: x["material"])


# --- закрытие: КС-2 / КС-3 ----------------------------------------------------------------------


@dataclass
class ClosingRow:
    item_id: str
    ord: int
    code: str | None
    name: str
    unit: str
    estimate_quantity: Decimal
    closed_before: Decimal
    period_quantity: Decimal
    include_quantity: Decimal
    unit_price: Decimal | None
    amount: Decimal
    volume_ids: list[str] = field(default_factory=list)


def closing_check(db: Session, contract: Contract, start: dt.date, end: dt.date, *,
                  only_confirmed: bool = True) -> dict[str, Any]:
    statuses = ["confirmed"] if only_confirmed else ["confirmed", "reported"]
    items = db.scalars(select(EstimateItem).where(EstimateItem.contract_id == contract.id).order_by(EstimateItem.ord))
    rows: list[ClosingRow] = []
    issues: list[dict[str, str]] = []
    for it in items:
        vols = db.scalars(select(WorkVolume).where(WorkVolume.estimate_item_id == it.id, WorkVolume.ks2_document_id.is_(None),
                                                   WorkVolume.date.between(start, end))).all()
        unconfirmed = [v for v in vols if v.status == "reported" and only_confirmed]
        if unconfirmed:
            issues.append({"level": "warning", "field": it.name,
                           "message": f"Не подтверждено ПТО: {sum(v.quantity for v in unconfirmed)} {it.unit} — «{it.name[:80]}» не включено"})
        vols = [v for v in vols if v.status in statuses]
        if not vols:
            continue
        if it.requires_aosr:
            ok = []
            for v in vols:
                aosr = db.get(Document, v.aosr_document_id) if v.aosr_document_id else None
                if aosr is None or aosr.status not in ("approved", "signed"):
                    issues.append({"level": "error", "field": it.name,
                                   "message": f"Скрытые работы без утверждённого АОСР: «{it.name[:80]}» "
                                              f"{v.quantity} {it.unit} ({v.date:%d.%m.%Y}) — не включено"})
                else:
                    ok.append(v)
            vols = ok
            if not vols:
                continue
        closed_before = db.scalar(select(func.coalesce(func.sum(WorkVolume.quantity), 0)).where(
            WorkVolume.estimate_item_id == it.id, WorkVolume.status == "closed")) or D0
        period_qty = sum((v.quantity for v in vols), D0)
        remaining = it.quantity - closed_before
        include = min(period_qty, max(remaining, D0))
        if period_qty > remaining:
            issues.append({"level": "warning", "field": it.name,
                           "message": f"Превышение сметного объёма «{it.name[:80]}»: сверх сметы {q4(period_qty - remaining)} "
                                      f"{it.unit} — требуется дополнительное соглашение, в КС-2 не включено"})
        if it.unit_price is None:
            issues.append({"level": "error", "field": it.name, "message": f"Нет цены за единицу: «{it.name[:80]}» — не включено"})
            continue
        if include <= 0:
            continue
        rows.append(ClosingRow(item_id=str(it.id), ord=it.ord, code=it.code, name=it.name, unit=it.unit,
                               estimate_quantity=it.quantity, closed_before=Decimal(closed_before),
                               period_quantity=q4(period_qty), include_quantity=q4(include), unit_price=it.unit_price,
                               amount=q2(include * it.unit_price), volume_ids=[str(v.id) for v in vols]))
    total = q2(sum((r.amount for r in rows), D0))
    vat_rate = contract.vat_rate if contract.vat_rate is not None else Decimal("20")
    vat = q2(total * vat_rate / 100)
    return {"rows": [asdict(r) for r in rows], "issues": issues, "total": total, "vat_rate": vat_rate, "vat": vat,
            "total_with_vat": total + vat, "period": [start, end]}


def ks3_totals(db: Session, contract: Contract, period_total: Decimal, end: dt.date) -> dict[str, Decimal]:
    """Стоимость с начала работ, с начала года и за период — по утверждённым КС-2 договора и текущему акту."""
    approved = db.scalars(select(Document).where(Document.contract_id == contract.id, Document.kind == "ks2",
                                                 Document.status.in_(["approved", "signed"]))).all()
    since_start = sum((Decimal(str(d.data.get("total", 0))) for d in approved), D0) + period_total
    since_year = sum((Decimal(str(d.data.get("total", 0))) for d in approved if d.date and d.date.year == end.year), D0) + period_total
    return {"since_start": q2(since_start), "since_year": q2(since_year), "period": q2(period_total)}


def on_ks2_status(db: Session, doc: Document, status: str) -> None:
    """Утверждён КС-2 — объёмы закрыты; отклонён — объёмы освобождаются для следующего акта."""
    for v in db.scalars(select(WorkVolume).where(WorkVolume.ks2_document_id == doc.id)):
        if status == "approved":
            v.status = "closed"
        elif status == "rejected":
            v.ks2_document_id = None
