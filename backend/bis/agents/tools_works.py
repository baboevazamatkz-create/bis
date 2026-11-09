"""Инструменты учёта работ, материалов и закрытия: объёмы, план-факт, М-29, списание, КС-2/КС-3."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from bis.agents.base import AgentContext, tool
from bis.agents.builtin_tools import _doc_ref, _uuid
from bis.agents.resolve import OBJ_HINT, contract_ref, contract_row, obj_row, object_ref
from bis.models import (
    Contract,
    Document,
    EstimateItem,
    Material,
    MaterialReceipt,
    MaterialUsage,
    WorkVolume,
)
from bis.services import closing as csvc
from bis.services import works as wsvc
from bis.services.audit import audit


def _owned(ctx: AgentContext, model, value: str, what: str):
    row = ctx.db.get(model, _uuid(value, what))
    if row is None or row.organization_id != ctx.org_id:
        raise LookupError(f"Не найден(а): {what}")
    return row


def _item_out(it: EstimateItem, score: float | None = None) -> dict:
    d = {"id": str(it.id), "code": it.code, "name": it.name, "unit": it.unit, "quantity": it.quantity,
         "unit_price": it.unit_price, "section": it.section, "requires_aosr": it.requires_aosr}
    if score is not None:
        d["match"] = round(score, 2)
    return d


class FindItemsArgs(BaseModel):
    query: str = Field(description="Описание работы своими словами: «бетонирование плиты Пм1»")
    object_id: str | None = None
    contract_id: str | None = None
    limit: int = Field(5, ge=1, le=20)


@tool("find_estimate_items", "Найти позиции сметы (ведомости объёмов) по описанию работы.", scopes=("registry:read",))
def find_estimate_items(ctx: AgentContext, args: FindItemsArgs) -> dict:
    found = wsvc.find_items(ctx.db, ctx.org_id, args.query,
                            object_id=object_ref(ctx, args.object_id) if args.object_id else None,
                            contract_id=contract_ref(ctx, args.contract_id) if args.contract_id else None, limit=args.limit)
    return {"items": [_item_out(it, s) for it, s in found]}


class ReportVolumeArgs(BaseModel):
    estimate_item_id: str
    quantity: float = Field(gt=0)
    date: dt.date | None = None
    location: str | None = Field(None, description="Оси, захватка, этаж")
    note: str | None = None


@tool("report_volume", "Записать выполненный объём работ (факт от прораба). Объём ждёт подтверждения ПТО.",
      scopes=("documents:write",))
def report_volume(ctx: AgentContext, args: ReportVolumeArgs) -> dict:
    it = _owned(ctx, EstimateItem, args.estimate_item_id, "позиция сметы")
    if it.object_id is None:
        raise ValueError("Позиция сметы не привязана к объекту")
    qty = Decimal(str(args.quantity))
    done = ctx.db.scalar(select(func.coalesce(func.sum(WorkVolume.quantity), 0)).where(
        WorkVolume.estimate_item_id == it.id)) or Decimal(0)
    v = WorkVolume(organization_id=ctx.org_id, object_id=it.object_id, estimate_item_id=it.id,
                   date=args.date or dt.date.today(), quantity=qty, location=args.location, note=args.note,
                   reported_by=f"{ctx.user.full_name} (через агента {ctx.agent_name})")
    ctx.db.add(v)
    ctx.db.flush()
    total = Decimal(done) + qty
    warnings = []
    if total > it.quantity:
        warnings.append(f"Накопленный объём {total} {it.unit} превышает сметный {it.quantity} {it.unit}")
    if it.requires_aosr:
        warnings.append("Скрытые работы: для закрытия в КС-2 нужен утверждённый АОСР")
    audit(ctx.db, ctx.org_id, actor_type="agent", actor=ctx.agent_name, action="volume.report", entity_type="work_volume",
          entity_id=v.id, details={"item": it.name[:100], "quantity": float(qty)})
    return {"volume_id": str(v.id), "item": it.name, "quantity": qty, "unit": it.unit, "cumulative": total,
            "estimate_quantity": it.quantity, "percent": float(round(total / it.quantity * 100, 1)) if it.quantity else None,
            "warnings": warnings, "status": "reported"}


class ConfirmArgs(BaseModel):
    volume_ids: list[str]
    aosr_document_id: str | None = Field(None, description="АОСР, подтверждающий скрытые работы")


@tool("confirm_volumes", "Подтвердить выполненные объёмы (ПТО) и при необходимости привязать АОСР.", scopes=("registry:write",))
def confirm_volumes(ctx: AgentContext, args: ConfirmArgs) -> dict:
    aosr = _owned(ctx, Document, args.aosr_document_id, "АОСР") if args.aosr_document_id else None
    if aosr is not None and aosr.kind != "aosr":
        raise ValueError("Указанный документ не является АОСР")
    out = []
    for vid in args.volume_ids:
        v = _owned(ctx, WorkVolume, vid, "объём")
        if v.status == "closed":
            raise ValueError("Объём уже закрыт в КС-2")
        v.status = "confirmed"
        if aosr:
            v.aosr_document_id = aosr.id
        out.append(str(v.id))
    audit(ctx.db, ctx.org_id, actor_type="agent", actor=ctx.agent_name, action="volume.confirm",
          details={"volumes": out, "aosr": args.aosr_document_id})
    return {"confirmed": out}


class ListVolumesArgs(BaseModel):
    object_id: str | None = None
    estimate_item_id: str | None = None
    status: Literal["reported", "confirmed", "closed"] | None = None
    period_start: dt.date | None = None
    period_end: dt.date | None = None
    limit: int = Field(50, ge=1, le=300)


@tool("list_volumes", "Журнал выполненных объёмов с фильтрами.", scopes=("registry:read",))
def list_volumes(ctx: AgentContext, args: ListVolumesArgs) -> dict:
    stmt = select(WorkVolume, EstimateItem).join(EstimateItem, EstimateItem.id == WorkVolume.estimate_item_id).where(
        WorkVolume.organization_id == ctx.org_id)
    if args.object_id:
        stmt = stmt.where(WorkVolume.object_id == object_ref(ctx, args.object_id))
    if args.estimate_item_id:
        stmt = stmt.where(WorkVolume.estimate_item_id == _uuid(args.estimate_item_id, "позиции"))
    if args.status:
        stmt = stmt.where(WorkVolume.status == args.status)
    if args.period_start:
        stmt = stmt.where(WorkVolume.date >= args.period_start)
    if args.period_end:
        stmt = stmt.where(WorkVolume.date <= args.period_end)
    rows = ctx.db.execute(stmt.order_by(WorkVolume.date.desc()).limit(args.limit)).all()
    return {"volumes": [{"id": str(v.id), "date": v.date, "item": it.name, "quantity": v.quantity, "unit": it.unit,
                         "location": v.location, "status": v.status, "has_aosr": v.aosr_document_id is not None,
                         "requires_aosr": it.requires_aosr} for v, it in rows]}


class ObjectDateArgs(BaseModel):
    object_id: str | None = Field(None, description=OBJ_HINT)
    on_date: dt.date | None = None


@tool("plan_fact", "План-факт по графику производства работ: отставания, прогноз окончания.", scopes=("registry:read",))
def plan_fact_tool(ctx: AgentContext, args: ObjectDateArgs) -> dict:
    obj = obj_row(ctx, args.object_id)
    return wsvc.plan_fact(ctx.db, ctx.org_id, obj.id, args.on_date)


@tool("material_balance", "Остатки материалов на объекте: приход, расход, остаток, расхождения.", scopes=("registry:read",))
def material_balance_tool(ctx: AgentContext, args: ObjectDateArgs) -> dict:
    obj = obj_row(ctx, args.object_id)
    return {"materials": wsvc.material_balance(ctx.db, ctx.org_id, obj.id, args.on_date)}


class FindMaterialsArgs(BaseModel):
    query: str | None = None
    limit: int = Field(20, ge=1, le=100)


@tool("find_materials", "Справочник материалов (номенклатура).", scopes=("registry:read",))
def find_materials(ctx: AgentContext, args: FindMaterialsArgs) -> dict:
    stmt = select(Material).where(Material.organization_id == ctx.org_id)
    if args.query:
        q = f"%{args.query}%"
        stmt = stmt.where(or_(Material.name.ilike(q), Material.code.ilike(q)))
    return {"materials": [{"id": str(m.id), "name": m.name, "unit": m.unit, "code": m.code}
                          for m in ctx.db.scalars(stmt.limit(args.limit))]}


class UsageArgs(BaseModel):
    object_id: str | None = Field(None, description=OBJ_HINT)
    material_id: str
    quantity: float = Field(gt=0)
    date: dt.date | None = None
    estimate_item_id: str | None = None
    note: str | None = None


@tool("record_material_usage", "Записать фактический расход (отпуск в работу) материала.", scopes=("documents:write",))
def record_material_usage(ctx: AgentContext, args: UsageArgs) -> dict:
    obj = obj_row(ctx, args.object_id)
    mat = _owned(ctx, Material, args.material_id, "материал")
    u = MaterialUsage(organization_id=ctx.org_id, object_id=obj.id, material_id=mat.id, quantity=Decimal(str(args.quantity)),
                      date=args.date or dt.date.today(), note=args.note,
                      estimate_item_id=_uuid(args.estimate_item_id, "позиции") if args.estimate_item_id else None)
    ctx.db.add(u)
    ctx.db.flush()
    balance = next((b for b in wsvc.material_balance(ctx.db, ctx.org_id, obj.id) if b["material_id"] == str(mat.id)), None)
    return {"usage_id": str(u.id), "material": mat.name, "quantity": u.quantity, "unit": mat.unit, "balance": balance}


class ReceiptArgs(BaseModel):
    object_id: str | None = Field(None, description=OBJ_HINT)
    material_id: str
    quantity: float = Field(gt=0)
    date: dt.date | None = None
    price: float | None = Field(None, description="Цена за единицу без НДС")
    document_ref: str | None = Field(None, description="УПД/накладная: номер и дата")
    quality_doc: str | None = Field(None, description="Сертификат/паспорт качества")


@tool("record_material_receipt", "Записать поступление материала на объект (по УПД/накладной).", scopes=("documents:write",))
def record_material_receipt(ctx: AgentContext, args: ReceiptArgs) -> dict:
    obj = obj_row(ctx, args.object_id)
    mat = _owned(ctx, Material, args.material_id, "материал")
    r = MaterialReceipt(organization_id=ctx.org_id, object_id=obj.id, material_id=mat.id,
                        quantity=Decimal(str(args.quantity)), date=args.date or dt.date.today(),
                        price=Decimal(str(args.price)) if args.price is not None else None, document_ref=args.document_ref,
                        quality_doc=args.quality_doc)
    ctx.db.add(r)
    ctx.db.flush()
    return {"receipt_id": str(r.id), "material": mat.name, "quantity": r.quantity, "unit": mat.unit,
            "warning": None if args.quality_doc else "Нет документа о качестве — понадобится для АОСР"}


class PeriodArgs(BaseModel):
    object_id: str | None = Field(None, description=OBJ_HINT)
    period_start: dt.date
    period_end: dt.date
    create_document: bool = Field(False, description="Сформировать файл М-29 в реестре документов")


@tool("m29_report", "Расход материалов по нормам и факт за период (М-29): перерасход, экономия, нехватка данных.",
      scopes=("registry:read",))
def m29_report(ctx: AgentContext, args: PeriodArgs) -> dict:
    obj = obj_row(ctx, args.object_id)
    if args.create_document:
        doc, data = csvc.create_m29(ctx.db, ctx.storage, ctx.org_id, obj.id, args.period_start, args.period_end,
                                    actor=f"agent:{ctx.agent_name}", signer=ctx.user.full_name)
        ref = _doc_ref(doc)
        ctx.documents.append(ref)
        return csvc._jsonable(data) | {"document": ref}
    return csvc._jsonable(wsvc.m29(ctx.db, ctx.org_id, obj.id, args.period_start, args.period_end))


class WriteOffArgs(BaseModel):
    object_id: str | None = Field(None, description=OBJ_HINT)
    period_start: dt.date
    period_end: dt.date
    commission: list[str] = Field(default_factory=list, description="Члены комиссии: должность, ФИО")


@tool("create_writeoff_act", "Сформировать акт списания материалов за период (в пределах норм; перерасход отдельно).",
      scopes=("documents:write",))
def create_writeoff_act(ctx: AgentContext, args: WriteOffArgs) -> dict:
    obj = obj_row(ctx, args.object_id)
    doc, act = csvc.create_writeoff(ctx.db, ctx.storage, ctx.org_id, obj.id, args.period_start, args.period_end,
                                    actor=f"agent:{ctx.agent_name}", commission=args.commission)
    ref = _doc_ref(doc)
    ctx.documents.append(ref)
    return csvc._jsonable(act) | {"document": ref, "issues": doc.issues}


class ClosingArgs(BaseModel):
    contract_id: str | None = None  # не задан — единственный договор или тот, где есть незакрытые объёмы
    period_start: dt.date | None = None  # не задан — с первого незакрытого объёма
    period_end: dt.date | None = None  # не задан — по сегодня
    only_confirmed: bool = True


def _closing_target(ctx: AgentContext, args: ClosingArgs) -> tuple[Contract, dt.date, dt.date, list[str]]:
    """Договор и период: что не задано — подставляем сами и сообщаем об этом."""
    assumed = []
    if args.contract_id or ctx.focus.get("contract"):
        c = contract_row(ctx, args.contract_id)
    else:
        open_by_contract = dict(ctx.db.execute(
            select(EstimateItem.contract_id, func.count(WorkVolume.id))
            .join(WorkVolume, WorkVolume.estimate_item_id == EstimateItem.id)
            .where(EstimateItem.organization_id == ctx.org_id, WorkVolume.ks2_document_id.is_(None))
            .group_by(EstimateItem.contract_id)).all())
        contracts = ctx.db.scalars(select(Contract).where(Contract.organization_id == ctx.org_id)
                                   .order_by(Contract.created_at.desc())).all()
        if not contracts:
            raise LookupError("В базе нет договоров — загрузите договор и смету")
        active = [k for k in contracts if k.status == "active"] or contracts
        with_volumes = [k for k in active if open_by_contract.get(k.id, 0)]
        # Multiple equally applicable contracts need concrete choices, not an arbitrary latest row.
        c = with_volumes[0] if len(with_volumes) == 1 else contract_row(ctx, None)
        assumed.append(f"договор № {c.number}" + (" (единственный)" if len(contracts) == 1 else " (с незакрытыми объёмами)"))
    end = args.period_end or dt.date.today()
    start = args.period_start
    if start is None:
        first = ctx.db.scalar(select(func.min(WorkVolume.date)).join(EstimateItem, WorkVolume.estimate_item_id == EstimateItem.id)
                              .where(EstimateItem.contract_id == c.id, WorkVolume.ks2_document_id.is_(None)))
        start = min(first, end) if first else end.replace(day=1)
        assumed.append(f"период {start:%d.%m.%Y}–{end:%d.%m.%Y} (все незакрытые объёмы)")
    return c, start, end, assumed


@tool("closing_check", "Проверка закрытия: что войдёт в КС-2, что нет и почему. Договор и период можно не задавать — подставятся.",
      scopes=("registry:read",))
def closing_check_tool(ctx: AgentContext, args: ClosingArgs) -> dict:
    c, start, end, assumed = _closing_target(ctx, args)
    out = csvc._jsonable(wsvc.closing_check(ctx.db, c, start, end, only_confirmed=args.only_confirmed))
    return out | {"contract": f"№ {c.number}", "period": f"{start:%d.%m.%Y}–{end:%d.%m.%Y}", "assumed": assumed}


@tool("create_ks2_ks3", "Черновики КС-2 и КС-3 из подтверждённых объёмов. Договор и период можно не задавать — подставятся.",
      scopes=("documents:write",))
def create_ks2_ks3_tool(ctx: AgentContext, args: ClosingArgs) -> dict:
    c, start, end, assumed = _closing_target(ctx, args)
    res = csvc.create_ks2_ks3(ctx.db, ctx.storage, c, start, end, actor=f"agent:{ctx.agent_name}",
                              only_confirmed=args.only_confirmed)
    refs = [_doc_ref(res["ks2"]), _doc_ref(res["ks3"])]
    ctx.documents.extend(refs)
    return {"documents": refs, "contract": f"№ {c.number}", "period": f"{start:%d.%m.%Y}–{end:%d.%m.%Y}", "assumed": assumed,
            "total": float(res["check"]["total"]), "vat": float(res["check"]["vat"]),
            "issues": res["check"]["issues"], "ks3_totals": csvc._jsonable(res["totals"])}


WORKS_TOOLS = [find_estimate_items, report_volume, confirm_volumes, list_volumes, plan_fact_tool, material_balance_tool,
               find_materials, record_material_usage, record_material_receipt, m29_report, create_writeoff_act,
               closing_check_tool, create_ks2_ks3_tool]


# --- исполнительные схемы -----------------------------------------------------------------------


class SurveyDocArgs(BaseModel):
    document_id: str = Field(description="Документ с файлом съёмки (CSV/XLSX) из реестра")
    geodetic: bool = Field(False, description="Координаты геодезические (X — на север)")


@tool("parse_survey_document", "Прочитать точки съёмки (проект/факт) из файла геодезиста в реестре.",
      scopes=("documents:read",))
def parse_survey_document(ctx: AgentContext, args: SurveyDocArgs) -> dict:
    from bis.cad.scheme import parse_survey

    doc = _owned(ctx, Document, args.document_id, "документ")
    if not doc.file_key:
        raise ValueError("У документа нет файла")
    points = parse_survey(ctx.storage.get(doc.file_key), doc.file_name or "survey.csv", geodetic=args.geodetic)
    return {"points": [p.model_dump() for p in points][:200], "count": len(points)}


class SchemeArgs(BaseModel):
    title: str
    object_id: str | None = None
    points: list[dict] = Field(description="Точки: name, design_x, design_y, fact_x, fact_y, design_z, fact_z (м)")
    x_axes: list[dict] = Field(default_factory=list, description="Поперечные оси: name, coord (м)")
    y_axes: list[dict] = Field(default_factory=list, description="Продольные оси: name, coord (м)")
    contour: list[list[float]] = Field(default_factory=list, description="Проектный контур [[x, y], ...], м")
    tolerance_plan_mm: float = 15
    tolerance_height_mm: float = 10
    code: str = ""
    surveyor: str = ""
    checker: str = ""
    notes: list[str] = Field(default_factory=list)


@tool("create_executive_scheme", "Построить исполнительную схему в DXF (AutoCAD/nanoCAD) по точкам съёмки: оси, "
      "отклонения, отметки, проверка допусков, штамп.", scopes=("documents:write",))
def create_executive_scheme(ctx: AgentContext, args: SchemeArgs) -> dict:
    from bis.cad.scheme import SchemeData
    from bis.models import Organization
    from bis.services.schemes import create_scheme

    obj = obj_row(ctx, args.object_id) if args.object_id else None
    org = ctx.db.get(Organization, ctx.org_id)
    data = SchemeData(title=args.title, object_name=(obj.name if obj else ""), organization=org.name, code=args.code,
                      surveyor=args.surveyor, checker=args.checker or ctx.user.full_name,
                      date=dt.date.today().strftime("%d.%m.%Y"), x_axes=args.x_axes, y_axes=args.y_axes,
                      contour=[tuple(c) for c in args.contour], points=args.points,
                      tolerance_plan_mm=args.tolerance_plan_mm, tolerance_height_mm=args.tolerance_height_mm,
                      notes=args.notes)
    doc = create_scheme(ctx.db, ctx.storage, ctx.org_id, data, actor=f"agent:{ctx.agent_name}",
                        object_id=obj.id if obj else None)
    ref = _doc_ref(doc)
    ctx.documents.append(ref)
    return ref | {"summary": doc.data["summary"], "scale": f"1:{doc.data['scale']}"}


WORKS_TOOLS += [parse_survey_document, create_executive_scheme]
