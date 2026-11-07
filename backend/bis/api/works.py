"""Учёт работ, материалов и закрытие: импорт сметы, план-факт, подтверждение объёмов, М-29, списание, КС-2/КС-3."""

import datetime as dt
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from bis.api.deps import require
from bis.api.schemas import DocumentOut
from bis.cad.scheme import SchemeData
from bis.cad.scheme import parse_survey as parse_survey_file
from bis.db import get_db
from bis.models import ConstructionObject, Contract, Document, User, WorkVolume
from bis.services import closing as csvc
from bis.services import schemes as ssvc
from bis.services import works as wsvc
from bis.services.audit import audit
from bis.services.storage import get_storage

router = APIRouter(tags=["works"])


def _owned(db: Session, user: User, model, item_id: uuid.UUID, what: str):
    row = db.get(model, item_id)
    if row is None or row.organization_id != user.organization_id:
        raise HTTPException(404, f"{what} не найден(а)")
    return row


@router.post("/contracts/{contract_id}/estimate/import")
def import_estimate(contract_id: uuid.UUID, file: UploadFile = File(...), replace: bool = Form(False),
                          db: Session = Depends(get_db), user: User = Depends(require("registry:write"))):
    """Импорт сметы / ведомости объёмов из Excel: заголовки определяются автоматически."""
    c = _owned(db, user, Contract, contract_id, "Договор")
    try:
        items = csvc.import_estimate(db, c, file.file.read(), replace=replace)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit(db, user.organization_id, actor_type="user", actor=str(user.id), action="estimate.import",
          entity_type="contract", entity_id=c.id, details={"items": len(items), "file": file.filename})
    db.commit()
    return {"imported": len(items), "hidden_works": sum(1 for i in items if i.requires_aosr),
            "total": float(sum((i.quantity * (i.unit_price or 0)) for i in items))}


@router.get("/objects/{object_id}/plan-fact")
def plan_fact(object_id: uuid.UUID, on_date: dt.date | None = None, db: Session = Depends(get_db),
              user: User = Depends(require("registry:read"))):
    obj = _owned(db, user, ConstructionObject, object_id, "Объект")
    return wsvc.plan_fact(db, user.organization_id, obj.id, on_date)


@router.get("/objects/{object_id}/materials/balance")
def balance(object_id: uuid.UUID, on_date: dt.date | None = None, db: Session = Depends(get_db),
            user: User = Depends(require("registry:read"))):
    obj = _owned(db, user, ConstructionObject, object_id, "Объект")
    return wsvc.material_balance(db, user.organization_id, obj.id, on_date)


class ConfirmIn(BaseModel):
    volume_ids: list[uuid.UUID]
    aosr_document_id: uuid.UUID | None = None


@router.post("/volumes/confirm")
def confirm(body: ConfirmIn, db: Session = Depends(get_db), user: User = Depends(require("registry:write"))):
    aosr = _owned(db, user, Document, body.aosr_document_id, "АОСР") if body.aosr_document_id else None
    if aosr is not None and aosr.kind != "aosr":
        raise HTTPException(400, "Документ не является АОСР")
    for vid in body.volume_ids:
        v = _owned(db, user, WorkVolume, vid, "Объём")
        if v.status == "closed":
            raise HTTPException(400, "Объём уже закрыт в КС-2")
        v.status = "confirmed"
        if aosr:
            v.aosr_document_id = aosr.id
    audit(db, user.organization_id, actor_type="user", actor=str(user.id), action="volume.confirm",
          details={"volumes": [str(v) for v in body.volume_ids]})
    db.commit()
    return {"confirmed": len(body.volume_ids)}


class PeriodIn(BaseModel):
    period_start: dt.date
    period_end: dt.date


class ObjectPeriodIn(PeriodIn):
    object_id: uuid.UUID
    commission: list[str] = []


@router.post("/materials/m29/preview")
def m29_preview(body: ObjectPeriodIn, db: Session = Depends(get_db), user: User = Depends(require("registry:read"))):
    obj = _owned(db, user, ConstructionObject, body.object_id, "Объект")
    return csvc._jsonable(wsvc.m29(db, user.organization_id, obj.id, body.period_start, body.period_end))


@router.post("/materials/m29", response_model=DocumentOut, status_code=201)
def m29_create(body: ObjectPeriodIn, db: Session = Depends(get_db), user: User = Depends(require("documents:write"))):
    obj = _owned(db, user, ConstructionObject, body.object_id, "Объект")
    doc, _ = csvc.create_m29(db, get_storage(), user.organization_id, obj.id, body.period_start, body.period_end,
                             actor=f"user:{user.id}", signer=user.full_name)
    db.commit()
    return doc


@router.post("/materials/write-off", response_model=DocumentOut, status_code=201)
def writeoff(body: ObjectPeriodIn, db: Session = Depends(get_db), user: User = Depends(require("documents:write"))):
    obj = _owned(db, user, ConstructionObject, body.object_id, "Объект")
    try:
        doc, _ = csvc.create_writeoff(db, get_storage(), user.organization_id, obj.id, body.period_start,
                                      body.period_end, actor=f"user:{user.id}", commission=body.commission)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return doc


class ClosingIn(PeriodIn):
    only_confirmed: bool = True


@router.post("/contracts/{contract_id}/closing/check")
def closing_check(contract_id: uuid.UUID, body: ClosingIn, db: Session = Depends(get_db),
                  user: User = Depends(require("registry:read"))):
    c = _owned(db, user, Contract, contract_id, "Договор")
    return csvc._jsonable(wsvc.closing_check(db, c, body.period_start, body.period_end, only_confirmed=body.only_confirmed))


@router.post("/contracts/{contract_id}/closing/ks2", status_code=201)
def create_ks2(contract_id: uuid.UUID, body: ClosingIn, db: Session = Depends(get_db),
               user: User = Depends(require("documents:write"))):
    c = _owned(db, user, Contract, contract_id, "Договор")
    try:
        res = csvc.create_ks2_ks3(db, get_storage(), c, body.period_start, body.period_end, actor=f"user:{user.id}",
                                  only_confirmed=body.only_confirmed)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return {"ks2": DocumentOut.model_validate(res["ks2"]), "ks3": DocumentOut.model_validate(res["ks3"]),
            "total": float(res["check"]["total"]), "vat": float(res["check"]["vat"]), "issues": res["check"]["issues"]}


# --- исполнительные схемы -----------------------------------------------------------------------


class SchemeIn(SchemeData):
    object_id: uuid.UUID | None = None


@router.post("/schemes", response_model=DocumentOut, status_code=201)
def create_scheme(body: SchemeIn, db: Session = Depends(get_db), user: User = Depends(require("documents:write"))):
    obj = _owned(db, user, ConstructionObject, body.object_id, "Объект") if body.object_id else None
    data = SchemeData.model_validate(body.model_dump(exclude={"object_id"}))
    from bis.models import Organization

    data.organization = data.organization or db.get(Organization, user.organization_id).name
    data.object_name = data.object_name or (f"{obj.name}" + (f", {obj.address}" if obj and obj.address else "") if obj else "")
    data.checker = data.checker or f"{user.position or ''} {user.full_name}".strip()
    if not data.points:
        raise HTTPException(400, "Нет точек съёмки")
    doc = ssvc.create_scheme(db, get_storage(), user.organization_id, data, actor=f"user:{user.id}",
                             object_id=body.object_id)
    db.commit()
    return doc


class SchemeDraftIn(BaseModel):
    title: str = "Исполнительная схема"
    element: str = "конструкция"
    object_id: uuid.UUID | None = None
    code: str = ""
    x_axes: list[dict] = []
    y_axes: list[dict] = []
    contour: list[list[float]] | None = None
    design_z: float | None = None
    measurements_text: str = ""
    tolerance_plan_mm: float | None = None
    tolerance_height_mm: float | None = None
    visual_note: str = ""


@router.post("/schemes/draft", response_model=DocumentOut, status_code=201)
def draft_scheme(body: SchemeDraftIn, db: Session = Depends(get_db), user: User = Depends(require("documents:write"))):
    """Схема без геодезической съёмки: оси/контур по проекту, допуски по нормативу, факт — по замерам (текст, голос)."""
    from bis.llm.gateway import get_gateway
    from bis.rag.embeddings import get_embedder

    if body.object_id:
        _owned(db, user, ConstructionObject, body.object_id, "Объект")
    try:
        doc = ssvc.draft_scheme(db, get_storage(), get_gateway(), get_embedder(), user.organization_id, title=body.title,
                                element=body.element, x_axes=body.x_axes, y_axes=body.y_axes, contour=body.contour,
                                measurements_text=body.measurements_text, tolerance_plan_mm=body.tolerance_plan_mm,
                                tolerance_height_mm=body.tolerance_height_mm, visual_note=body.visual_note,
                                design_z=body.design_z, object_id=body.object_id, code=body.code, actor=f"user:{user.id}")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return doc


@router.post("/documents/{doc_id}/open-in-cad")
def open_in_cad(doc_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    """Открыть чертёж в запущенном AutoCAD или nanoCAD (BIS установлен на этом же компьютере с Windows)."""
    import tempfile

    from bis.cad import bridge
    from bis.models import Document

    doc = _owned(db, user, Document, doc_id, "Документ")
    if not doc.file_key or not (doc.file_name or "").lower().endswith((".dxf", ".dwg")):
        raise HTTPException(400, "У документа нет чертежа DXF/DWG")
    path = Path(tempfile.gettempdir()) / f"bis-{doc.id}{Path(doc.file_name).suffix}"
    path.write_bytes(get_storage().get(doc.file_key))

    def work() -> str:  # в потоке COM: из обычного потока сервера AutoCAD «не виден» (не вызван CoInitialize)
        cad = bridge.connect("auto")
        if cad.name == "dxf":
            reasons = "; ".join(bridge.LAST_ERRORS)[:400]
            raise bridge.CadError("AutoCAD или nanoCAD не найден на этом компьютере — откройте CAD (не от имени "
                                  "администратора) и повторите, или скачайте файл" + (f". Причина: {reasons}" if reasons else ""))
        cad.open_file(str(path))
        cad.zoom()
        return cad.name

    try:
        name = bridge.in_com_thread(work)
    except bridge.CadError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001 — ошибка COM: CAD занят диалогом и т.п.
        raise HTTPException(400, f"CAD не открыл файл: {e}. Закройте диалоговые окна в CAD и повторите") from e
    return {"opened_in": name, "file": str(path)}


@router.post("/schemes/survey")
def parse_survey(file: UploadFile = File(...), geodetic: bool = Form(False),
                       user: User = Depends(require("documents:read"))):
    """Разбор файла съёмки геодезиста (CSV/TXT/XLSX) в точки схемы."""
    try:
        points = parse_survey_file(file.file.read(), file.filename or "survey.csv", geodetic=geodetic)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"points": [p.model_dump() for p in points]}


# --- проектная документация ----------------------------------------------------------------------


@router.get("/objects/{object_id}/pd-completeness")
def pd_completeness(object_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    from bis.services.project_docs import completeness

    obj = _owned(db, user, ConstructionObject, object_id, "Объект")
    return completeness(db, user.organization_id, obj.id)


class CompareIn(BaseModel):
    contract_id: uuid.UUID
    tolerance_pct: float = 5.0


@router.post("/documents/{doc_id}/specification")
def specification(doc_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    from bis.agents.tools_pd import spec_for_document
    from bis.api.deps import agent_context

    doc = _owned(db, user, Document, doc_id, "Документ")
    spec, fallback = spec_for_document(agent_context(db, user), doc)
    db.commit()
    return {"items": [i.model_dump() for i in spec.items], "method": "rules" if fallback else "model"}


@router.post("/documents/{doc_id}/compare-estimate")
def compare_estimate(doc_id: uuid.UUID, body: CompareIn, db: Session = Depends(get_db),
                     user: User = Depends(require("documents:read"))):
    from bis.agents.tools_pd import spec_for_document
    from bis.api.deps import agent_context
    from bis.services.project_docs import compare_with_estimate

    doc = _owned(db, user, Document, doc_id, "Документ")
    contract = _owned(db, user, Contract, body.contract_id, "Договор")
    spec, _ = spec_for_document(agent_context(db, user), doc)
    db.commit()
    return compare_with_estimate(db, user.organization_id, contract.id, spec, body.tolerance_pct)


# --- исполнительная документация ----------------------------------------------------------------


@router.post("/documents/aook", response_model=DocumentOut, status_code=201)
def create_aook(body: dict, db: Session = Depends(get_db), user: User = Depends(require("documents:write"))):
    from bis.agents.tools_id import AOOKArgs, build_aook
    from bis.api.deps import agent_context
    from bis.services.exec_docs import create_aook as svc_create

    try:
        args = AOOKArgs.model_validate(body)
        data, obj = build_aook(agent_context(db, user), args)
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e)) from e
    doc = svc_create(db, get_storage(), org_id=user.organization_id, data=data, created_by=f"user:{user.id}",
                     object_id=obj.id)
    db.commit()
    return doc


@router.post("/documents/aook/check")
def check_aook(body: dict, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    from bis.agents.tools_id import AOOKArgs, build_aook
    from bis.api.deps import agent_context
    from bis.documents.aook import validate_aook

    try:
        data, _ = build_aook(agent_context(db, user), AOOKArgs.model_validate(body))
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e)) from e
    return {"issues": [i.model_dump() for i in validate_aook(data)],
            "representatives": {k: v.format() for k, v in data.representatives.items()}}


@router.get("/objects/{object_id}/id-registry")
def id_registry(object_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:read"))):
    from bis.services.exec_docs import id_registry as reg

    obj = _owned(db, user, ConstructionObject, object_id, "Объект")
    return reg(db, user.organization_id, obj.id)


@router.post("/objects/{object_id}/id-registry/export", response_model=DocumentOut, status_code=201)
def export_registry(object_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require("documents:write"))):
    from bis.services.exec_docs import export_registry as exp

    obj = _owned(db, user, ConstructionObject, object_id, "Объект")
    doc = exp(db, get_storage(), user.organization_id, obj.id, actor=f"user:{user.id}")
    db.commit()
    return doc


@router.post("/objects/{object_id}/work-journal", response_model=DocumentOut, status_code=201)
def export_journal(object_id: uuid.UUID, body: PeriodIn, db: Session = Depends(get_db),
                   user: User = Depends(require("documents:write"))):
    from bis.services.exec_docs import export_work_journal

    obj = _owned(db, user, ConstructionObject, object_id, "Объект")
    try:
        doc = export_work_journal(db, get_storage(), user.organization_id, obj.id, body.period_start, body.period_end,
                                  actor=f"user:{user.id}")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return doc


@router.post("/archive/import")
def archive_import(file: UploadFile = File(...), object_id: uuid.UUID | None = Form(None), folder: str = Form(""),
                         analyze_contracts: bool = Form(True), db: Session = Depends(get_db),
                         user: User = Depends(require("documents:write"))):
    """Загрузка архива по одному файлу: тип определяет ИИ, документ — в реестр и базу знаний, договор — на контроль."""
    from bis.llm.gateway import get_gateway
    from bis.rag.embeddings import get_embedder
    from bis.services.archive import import_file

    if object_id:
        _owned(db, user, ConstructionObject, object_id, "Объект")
    data = file.file.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(413, "Файл больше 100 МБ")
    try:
        r = import_file(db, get_storage(), get_gateway(), get_embedder(), user.organization_id, data=data,
                        name=file.filename or "file", mime=file.content_type, object_id=object_id, folder=folder,
                        analyze_contracts=analyze_contracts, actor=f"user:{user.id}")
    except Exception as e:  # noqa: BLE001 — один плохой файл не останавливает загрузку архива
        db.rollback()
        return {"file": file.filename, "status": "error", "error": str(e)[:300]}
    db.commit()
    return r
