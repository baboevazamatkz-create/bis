"""Формирование документов закрытия и учёта материалов: КС-2 + КС-3, М-29, акт списания; импорт сметы из Excel."""

from __future__ import annotations

import datetime as dt
import io
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.documents.forms_xlsx import render_ks2, render_ks3, render_m29, render_writeoff
from bis.models import (
    ConstructionObject,
    Contract,
    Counterparty,
    Document,
    EstimateItem,
    Organization,
    WorkVolume,
)
from bis.services import works as wsvc
from bis.services.documents import next_number, save_document
from bis.services.storage import Storage

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dt.date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    return obj


def _parties(db: Session, contract: Contract) -> dict[str, str | None]:
    org = db.get(Organization, contract.organization_id)
    cp = db.get(Counterparty, contract.counterparty_id) if contract.counterparty_id else None
    obj = db.get(ConstructionObject, contract.object_id) if contract.object_id else None
    developer = db.get(Counterparty, obj.developer_id) if obj and obj.developer_id else None
    ours = f"{org.name}, ИНН {org.inn}" if org.inn else org.name
    theirs = (f"{cp.name}, ИНН {cp.inn}" if cp and cp.inn else cp.name) if cp else "—"
    customer, contractor = (theirs, ours) if contract.our_role == "contractor" else (ours, theirs)
    return {"customer": customer, "contractor": contractor, "investor": developer.name if developer else None,
            "construction": obj.name if obj else "—", "object": (obj.address or obj.name) if obj else "—",
            "contract": f"№ {contract.number}" + (f" от {contract.date:%d.%m.%Y}" if contract.date else "")}


def create_ks2_ks3(db: Session, storage: Storage, contract: Contract, start: dt.date, end: dt.date, *, actor: str,
                   only_confirmed: bool = True, signers: tuple[str, str] = ("", "")) -> dict[str, Any]:
    check = wsvc.closing_check(db, contract, start, end, only_confirmed=only_confirmed)
    if not check["rows"]:
        raise ValueError("Нет объёмов, которые можно закрыть за период: " +
                         "; ".join(i["message"] for i in check["issues"][:5]) if check["issues"] else
                         "Нет объёмов, которые можно закрыть за период")
    p = _parties(db, contract)
    seq = next_number(db, contract.organization_id, f"ks2:{contract.id}")
    number = str(seq)
    today = dt.date.today()
    doc_date = min(today, end) if end < today else today
    ks2_bytes = render_ks2(number=number, date=doc_date, period=(start, end), investor=p["investor"], customer=p["customer"],
                           contractor=p["contractor"], construction=p["construction"], obj=p["object"],
                           contract=p["contract"], estimate_total=contract.amount, rows=check["rows"],
                           total=check["total"], vat_rate=check["vat_rate"], vat=check["vat"], signers=signers)
    ks2 = save_document(db, storage, org_id=contract.organization_id, kind="ks2",
                        title=f"КС-2 № {number} по договору № {contract.number} за {start:%d.%m}–{end:%d.%m.%Y}",
                        data=_jsonable({"rows": check["rows"], "total": check["total"], "vat": check["vat"],
                                        "vat_rate": check["vat_rate"], "period": [start, end]}),
                        file_bytes=ks2_bytes, file_name=f"КС-2_{number}_{contract.number.replace('/', '-')}.xlsx",
                        created_by=actor, number=number, doc_date=doc_date, object_id=contract.object_id,
                        contract_id=contract.id, mime=XLSX_MIME,
                        # позиции с ошибками в акт не вошли — для самого акта это предупреждения, не блокирующие утверждение
                        issues=[i | {"level": "warning"} for i in check["issues"]])
    totals = wsvc.ks3_totals(db, contract, check["total"], end)
    ks3_bytes = render_ks3(number=number, date=doc_date, period=(start, end), investor=p["investor"], customer=p["customer"],
                           contractor=p["contractor"], construction=p["construction"], obj=p["object"],
                           contract=p["contract"], totals=totals, vat_rate=check["vat_rate"], signers=signers)
    ks3 = save_document(db, storage, org_id=contract.organization_id, kind="ks3",
                        title=f"КС-3 № {number} по договору № {contract.number} за {start:%d.%m}–{end:%d.%m.%Y}",
                        data=_jsonable({"totals": totals, "ks2_id": str(ks2.id), "vat_rate": check["vat_rate"]}),
                        file_bytes=ks3_bytes, file_name=f"КС-3_{number}_{contract.number.replace('/', '-')}.xlsx",
                        created_by=actor, number=number, doc_date=doc_date, object_id=contract.object_id,
                        contract_id=contract.id, mime=XLSX_MIME)
    for row in check["rows"]:
        for vid in row["volume_ids"]:
            db.get(WorkVolume, uuid.UUID(vid)).ks2_document_id = ks2.id
    db.flush()
    return {"ks2": ks2, "ks3": ks3, "check": check, "totals": totals}


def create_m29(db: Session, storage: Storage, org_id: uuid.UUID, object_id: uuid.UUID, start: dt.date, end: dt.date,
               *, actor: str, signer: str = "") -> tuple[Document, dict[str, Any]]:
    data = wsvc.m29(db, org_id, object_id, start, end)
    org = db.get(Organization, org_id)
    obj = db.get(ConstructionObject, object_id)
    content = render_m29(org=org.name, obj=obj.name, period=(start, end), rows=_jsonable(data["rows"]), signer=signer)
    doc = save_document(db, storage, org_id=org_id, kind="m29", title=f"М-29 {obj.code or obj.name[:40]} за {start:%d.%m}–{end:%d.%m.%Y}",
                        data=_jsonable(data), file_bytes=content, file_name=f"М-29_{start:%Y-%m}.xlsx", created_by=actor,
                        doc_date=end, object_id=object_id, issues=data["issues"], mime=XLSX_MIME)
    return doc, data


def create_writeoff(db: Session, storage: Storage, org_id: uuid.UUID, object_id: uuid.UUID, start: dt.date, end: dt.date,
                    *, actor: str, commission: list[str] | None = None) -> tuple[Document, dict[str, Any]]:
    data = wsvc.m29(db, org_id, object_id, start, end)
    act = wsvc.write_off(data)
    if not act["rows"]:
        raise ValueError("Нечего списывать: нет фактического расхода с нормами за период")
    org = db.get(Organization, org_id)
    obj = db.get(ConstructionObject, object_id)
    number = str(next_number(db, org_id, f"writeoff:{end.year}"))
    content = render_writeoff(number=number, date=end, org=org.name, obj=obj.name, period=(start, end),
                              rows=_jsonable(act["rows"]), total=act["total"], commission=commission or [])
    excess = [r for r in act["rows"] if r["excess"] > 0]
    issues = [{"level": "warning", "field": r["material"],
               "message": f"Сверх нормы не списано {r['excess']} {r['unit']} {r['material']} — нужна служебная записка"}
              for r in excess]
    doc = save_document(db, storage, org_id=org_id, kind="writeoff", title=f"Акт списания № {number} ({obj.code or obj.name[:40]})",
                        data=_jsonable(act), file_bytes=content, file_name=f"Акт_списания_{number}.xlsx", created_by=actor,
                        number=number, doc_date=end, object_id=object_id, issues=issues, mime=XLSX_MIME)
    return doc, act


# --- импорт сметы -------------------------------------------------------------------------------

HEADER_KEYS = {
    "ord": r"^№\s*(п/?п|поз)?|^n\b|номер\s+позиц|^№$",
    "code": r"шифр|обоснован|расценк|код",
    "name": r"наименован",
    "unit": r"ед\.?\s*изм|единиц",
    "quantity": r"кол-?во|количеств|объ[её]м",
    "price": r"цена|стоимость\s+ед|за\s+ед",
    "total": r"^(стоимость|сумма|всего)",
}


def _num(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, int | float | Decimal):
        return Decimal(str(v))
    s = re.sub(r"[\s ]", "", str(v)).replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_estimate_xlsx(data: bytes) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Ищет строку заголовка по ключевым словам и разбирает позиции сметы/ведомости объёмов."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    cols: dict[str, int] = {}
    header_row = None
    for i, row in enumerate(rows[:40]):
        found: dict[str, int] = {}
        for j, cell in enumerate(row):
            text = str(cell or "").strip().lower()
            if not text:
                continue
            for key, pattern in HEADER_KEYS.items():
                if key not in found and re.search(pattern, text):
                    found[key] = j
                    break
        if "name" in found and "quantity" in found:
            cols, header_row = found, i
            break
    if header_row is None:
        raise ValueError("Не найдена строка заголовка (нужны столбцы «Наименование» и «Количество»)")
    items, section = [], None
    for row in rows[header_row + 1:]:
        def get(key: str, _row=row) -> Any:
            j = cols.get(key)
            return _row[j] if j is not None and j < len(_row) else None
        name = str(get("name") or "").strip()
        qty = _num(get("quantity"))
        if not name:
            continue
        if qty is None:
            if re.match(r"(раздел|глава|этап|итого|всего)", name.lower()):
                section = None if name.lower().startswith(("итого", "всего")) else name
            elif not any(get(k) for k in ("unit", "price", "total")):
                section = name
            continue
        if re.match(r"^\d+(\.0)?$", name) or qty <= 0:
            continue  # строка с номерами столбцов
        price = _num(get("price"))
        total = _num(get("total"))
        if price is None and total is not None and qty:
            price = (total / qty).quantize(Decimal("0.01"))
        items.append({"ord": len(items) + 1, "section": section, "code": str(get("code") or "").strip() or None,
                      "name": name, "unit": str(get("unit") or "").strip() or "шт", "quantity": qty, "unit_price": price,
                      "requires_aosr": bool(wsvc.HIDDEN_WORKS.search(name))})
    return items, cols


def import_estimate(db: Session, contract: Contract, data: bytes, *, replace: bool = False) -> list[EstimateItem]:
    items, _ = parse_estimate_xlsx(data)
    if not items:
        raise ValueError("В файле не найдено позиций с количеством")
    if replace:
        for it in db.scalars(select(EstimateItem).where(EstimateItem.contract_id == contract.id)):
            db.delete(it)
    created = []
    for it in items:
        row = EstimateItem(organization_id=contract.organization_id, contract_id=contract.id, object_id=contract.object_id,
                           **it)
        db.add(row)
        created.append(row)
    db.flush()
    return created
