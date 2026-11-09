"""Исполнительная документация объекта: АООК, реестр ИД с проверкой комплектности, журнал выполнения работ.

- Реестр ИД — перечень актов, схем и протоколов объекта (XLSX) с проверкой: на все скрытые работы с выполненными
  объёмами должен быть утверждённый АОСР.
- Журнал выполнения работ — сведения о выполнении работ (раздел 3 общего журнала работ, Приказ Минстроя
  № 1026/пр) из учтённых объёмов: дата, работа и место, объём, исполнитель, подтверждающий акт.
  Сверяйте столбцы с действующей редакцией формы.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.documents.aook import AOOKData, render_aook, validate_aook
from bis.documents.forms_xlsx import SheetBuilder
from bis.documents.templates import render_with_org_template
from bis.models import ConstructionObject, Document, EstimateItem, Organization, WorkVolume
from bis.services.documents import next_number, save_document
from bis.services.storage import Storage

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ID_KINDS = {"aosr": "АОСР", "aook": "АООК", "scheme": "Исполнительная схема", "test_protocol": "Протокол испытаний",
            "certificate": "Документ о качестве"}


def create_aook(db: Session, storage: Storage, *, org_id: uuid.UUID, data: AOOKData, created_by: str,
                object_id: uuid.UUID | None = None) -> Document:
    if not data.number:
        data.number = str(next_number(db, org_id, f"aook:{object_id or 'none'}"))
    data.date = data.date or dt.date.today()
    from bis.services.library_access import bind_context

    bind_context(db, org_id, object_id)
    issues = [i.model_dump() for i in validate_aook(data)]
    title = f"АООК № {data.number}: {data.works[:150]}"
    return save_document(db, storage, org_id=org_id, kind="aook", title=title, data=data.model_dump(mode="json"),
                         file_bytes=render_with_org_template(db, storage, org_id, "aook", data) or render_aook(data), file_name=f"АООК_{data.number}.docx", created_by=created_by,
                         number=data.number, doc_date=data.date, object_id=object_id, issues=issues)


def id_registry(db: Session, org_id: uuid.UUID, object_id: uuid.UUID) -> dict[str, Any]:
    docs = db.scalars(select(Document).where(Document.organization_id == org_id, Document.object_id == object_id,
                                             Document.kind.in_(list(ID_KINDS))).order_by(Document.date, Document.created_at)).all()
    rows = [{"id": str(d.id), "kind": d.kind, "kind_title": ID_KINDS[d.kind], "number": d.number, "date": d.date,
             "title": d.title, "status": d.status} for d in docs]
    issues: list[dict[str, str]] = []
    # покрытие скрытых работ актами
    vols = db.execute(select(WorkVolume, EstimateItem).join(EstimateItem, EstimateItem.id == WorkVolume.estimate_item_id)
                      .where(WorkVolume.organization_id == org_id, WorkVolume.object_id == object_id,
                             EstimateItem.requires_aosr.is_(True))).all()
    missing: dict[str, list[WorkVolume]] = defaultdict(list)
    for v, it in vols:
        aosr = db.get(Document, v.aosr_document_id) if v.aosr_document_id else None
        if aosr is None or aosr.status not in ("approved", "signed"):
            missing[it.name].append(v)
    for name, vs in missing.items():
        dates = sorted(v.date for v in vs)
        issues.append({"level": "error", "field": name,
                       "message": f"Скрытые работы без утверждённого АОСР: «{name[:90]}» — {len(vs)} записей объёмов "
                                  f"с {dates[0]:%d.%m.%Y} по {dates[-1]:%d.%m.%Y}"})
    drafts = [r for r in rows if r["status"] in ("draft", "rejected")]
    if drafts:
        issues.append({"level": "warning", "field": "status",
                       "message": f"Не утверждено документов ИД: {len(drafts)} (черновики/отклонённые)"})
    covered = sum(1 for v, _ in vols) - sum(len(x) for x in missing.values())
    return {"documents": rows, "issues": issues, "hidden_volumes": len(vols), "hidden_covered": covered,
            "coverage_percent": round(covered / len(vols) * 100, 1) if vols else None,
            "by_kind": {ID_KINDS[k]: sum(1 for r in rows if r["kind"] == k) for k in ID_KINDS}}


def export_registry(db: Session, storage: Storage, org_id: uuid.UUID, object_id: uuid.UUID, *, actor: str) -> Document:
    reg = id_registry(db, org_id, object_id)
    org = db.get(Organization, org_id)
    obj = db.get(ConstructionObject, object_id)
    s = SheetBuilder("Реестр ИД", [6, 22, 12, 12, 70, 16])
    s.line("РЕЕСТР ИСПОЛНИТЕЛЬНОЙ ДОКУМЕНТАЦИИ", bold=True, size=12, align="center")
    s.pair("Объект", obj.name + (f", {obj.address}" if obj.address else ""))
    s.pair("Лицо, осуществляющее строительство", org.name)
    s.pair("Дата формирования", dt.date.today().strftime("%d.%m.%Y"))
    s.row += 1
    s.header(["№", "Вид документа", "Номер", "Дата", "Наименование", "Статус"])
    status = {"draft": "черновик", "approved": "утверждён", "signed": "подписан", "rejected": "отклонён", "registered": "зарегистрирован"}
    for i, r in enumerate(reg["documents"], 1):
        s.data([i, r["kind_title"], r["number"] or "", r["date"].strftime("%d.%m.%Y") if r["date"] else "", r["title"],
                status.get(r["status"], r["status"])])
    s.row += 1
    for issue in reg["issues"]:
        s.line(("⚠ " if issue["level"] == "warning" else "✖ ") + issue["message"], size=9)
    s.signatures([("Ответственный за ведение ИД", "")])
    seq = next_number(db, org_id, f"id_registry:{object_id}")
    content = s.to_bytes()
    from bis.services.library_access import bind_context

    if bind_context(db, org_id, object_id).strict:
        from bis.services.library_forms import render

        content = render(db, storage, org_id, "id_registry", {"number": str(seq), "object_name": obj.name,
            "date": dt.date.today(), "rows": [[r["number"], r["kind_title"], r["date"], r["title"], r["status"]] for r in reg["documents"]]})
    return save_document(db, storage, org_id=org_id, kind="id_registry", title=f"Реестр ИД № {seq} ({obj.code or obj.name[:40]})",
                         data={"documents": len(reg["documents"]), "issues": reg["issues"]}, file_bytes=content,
                         file_name=f"Реестр_ИД_{seq}.xlsx", created_by=actor, number=str(seq), doc_date=dt.date.today(),
                         object_id=object_id, issues=reg["issues"], mime=XLSX_MIME)


def work_journal(db: Session, org_id: uuid.UUID, object_id: uuid.UUID, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
    org = db.get(Organization, org_id)
    rows = db.execute(select(WorkVolume, EstimateItem).join(EstimateItem, EstimateItem.id == WorkVolume.estimate_item_id)
                      .where(WorkVolume.organization_id == org_id, WorkVolume.object_id == object_id,
                             WorkVolume.date.between(start, end)).order_by(WorkVolume.date, WorkVolume.created_at)).all()
    out = []
    for v, it in rows:
        aosr = db.get(Document, v.aosr_document_id) if v.aosr_document_id else None
        out.append({"date": v.date, "work": it.name, "location": v.location, "quantity": v.quantity, "unit": it.unit,
                    "performer": org.name, "status": v.status, "act": f"АОСР № {aosr.number}" if aosr else None,
                    "note": v.note})
    return out


def export_work_journal(db: Session, storage: Storage, org_id: uuid.UUID, object_id: uuid.UUID, start: dt.date,
                        end: dt.date, *, actor: str) -> Document:
    entries = work_journal(db, org_id, object_id, start, end)
    if not entries:
        raise ValueError("За период нет записей о выполненных работах")
    obj = db.get(ConstructionObject, object_id)
    s = SheetBuilder("Журнал работ", [6, 12, 58, 26, 14, 26, 22])
    s.line("Сведения о выполнении работ в процессе строительства (раздел 3 общего журнала работ)", bold=True, size=11, align="center")
    s.pair("Объект", obj.name)
    s.pair("Период", f"с {start:%d.%m.%Y} по {end:%d.%m.%Y}")
    s.row += 1
    s.header(["№", "Дата", "Наименование работ", "Место (оси, отметки, захватка)", "Объём", "Исполнитель", "Подтверждение"])
    for i, e in enumerate(entries, 1):
        s.data([i, e["date"].strftime("%d.%m.%Y"), e["work"], e["location"] or "", f"{e['quantity']:g} {e['unit']}",
                e["performer"], e["act"] or ("подтверждено ПТО" if e["status"] != "reported" else "не подтверждено")])
    s.signatures([("Ответственный за ведение журнала", "")])
    content = s.to_bytes()
    from bis.services.library_access import bind_context

    if bind_context(db, org_id, object_id).strict:
        from bis.services.library_forms import render

        content = render(db, storage, org_id, "work_journal", {"object_name": obj.name, "date": end,
            "rows": [[e["date"], e["work"], e["location"], e["quantity"], e["unit"], e["performer"], e["act"]] for e in entries]})
    return save_document(db, storage, org_id=org_id, kind="work_journal",
                         title=f"Журнал выполнения работ {obj.code or obj.name[:40]} за {start:%d.%m}–{end:%d.%m.%Y}",
                         data={"entries": len(entries)}, file_bytes=content,
                         file_name=f"Журнал_работ_{start:%Y-%m}.xlsx", created_by=actor, doc_date=end,
                         object_id=object_id, mime=XLSX_MIME)
