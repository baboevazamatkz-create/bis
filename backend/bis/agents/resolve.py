"""Объект и договор по тому, как их называет человек или модель — без внутренних кодов.

- id (UUID) — как есть;
- номер договора («53901-22», «№ ГП-01/2026»), шифр или название объекта — поиском;
- ничего не указано: объект или договор один — он; несколько — ошибка со списком вариантов,
  чтобы ассистент спросил «этот или этот?», а не «предоставьте номер договора».
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select

from bis.models import ConstructionObject, Contract, Counterparty

OBJ_HINT = "id, шифр или название объекта; если объект один — можно не указывать"
CONTRACT_HINT = "id или номер договора («53901-22»), можно название контрагента; если договор один — можно не указывать"


class AmbiguousReference(LookupError):
    def __init__(self, message: str, candidates: list[dict]):
        self.candidates = candidates
        super().__init__(message)


def _as_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def obj_row(ctx, value: str | None) -> ConstructionObject:
    scope = ctx.db.info.get("source_context")
    if scope and scope.strict and scope.object_id:
        if value and _as_uuid(value) and _as_uuid(value) != scope.object_id:
            raise LookupError("Нельзя использовать другой проект в текущей задаче")
        value = str(scope.object_id)
    value = value or ctx.focus.get("object")
    objs = ctx.db.scalars(select(ConstructionObject).where(ConstructionObject.organization_id == ctx.org_id)
                          .order_by(ConstructionObject.created_at)).all()
    if value and (u := _as_uuid(value)):
        found = [o for o in objs if o.id == u]
    elif value:
        low = str(value).lower().strip()
        found = [o for o in objs if o.code and o.code.lower() == low] or \
                [o for o in objs if low in o.name.lower() or (o.code and low in o.code.lower())]
    else:
        found = [o for o in objs if o.status == "active"] or objs
    if len(found) == 1:
        return found[0]
    if not found:
        raise LookupError("Объект не найден" + (": " + "; ".join(f"{o.code or ''} {o.name}" for o in objs[:15])
                                                if objs else " — объектов в базе ещё нет"))
    candidates = [{"id": str(o.id), "name": o.name, "code": o.code} for o in found]
    raise AmbiguousReference("Несколько объектов — выберите из найденных: " + "; ".join(
        f"{o.code or ''} {o.name}".strip() for o in found[:15]), candidates)


def contract_row(ctx, value: str | None) -> Contract:
    value = value or ctx.focus.get("contract")
    contracts = ctx.db.scalars(select(Contract).where(Contract.organization_id == ctx.org_id)
                               .order_by(Contract.created_at)).all()
    if value and (u := _as_uuid(value)):
        found = [c for c in contracts if c.id == u]
    elif value:
        raw = re.sub(r"^\s*(договор\w*\s*)?№?\s*", "", str(value), flags=re.IGNORECASE).strip().lower()
        found = [c for c in contracts if c.number and c.number.lower() == raw] or \
                [c for c in contracts if c.number and raw and raw in c.number.lower()]
        if not found:  # по контрагенту: «договор с Сахаэнерго»
            cps = {cp.id: cp.name.lower() for cp in ctx.db.scalars(select(Counterparty).where(
                Counterparty.organization_id == ctx.org_id))}
            found = [c for c in contracts if c.counterparty_id in cps and raw and raw in cps[c.counterparty_id]]
    else:
        found = [c for c in contracts if c.status == "active"] or contracts
    if len(found) == 1:
        return found[0]
    if not found:
        raise LookupError("Договор не найден" + (": есть " + "; ".join(f"№ {c.number}" for c in contracts[:15])
                                                 if contracts else " — договоров в базе ещё нет"))
    candidates = [{"id": str(c.id), "number": c.number, "date": c.date, "subject": c.subject,
                   "object_id": str(c.object_id) if c.object_id else None} for c in found]
    raise AmbiguousReference("Несколько договоров — выберите из найденных: " + "; ".join(
        f"№ {c.number}" + (f" от {c.date:%d.%m.%Y}" if c.date else "")
        + (f" — {c.subject[:100]}" if c.subject else "") for c in found[:15]), candidates)


def object_ref(ctx, value: str | None) -> uuid.UUID:
    return obj_row(ctx, value).id


def contract_ref(ctx, value: str | None) -> uuid.UUID:
    return contract_row(ctx, value).id
