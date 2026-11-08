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


def _as_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def obj_row(ctx, value: str | None) -> ConstructionObject:
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
    raise LookupError("Несколько объектов — уточните у пользователя, какой: " + "; ".join(
        f"{o.code or ''} {o.name} (id {o.id})".strip() for o in found[:15]))


def contract_row(ctx, value: str | None) -> Contract:
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
    raise LookupError("Несколько договоров — уточните у пользователя, какой: " + "; ".join(
        f"№ {c.number}" + (f" от {c.date:%d.%m.%Y}" if c.date else "") + f" (id {c.id})" for c in found[:15]))


def object_ref(ctx, value: str | None) -> uuid.UUID:
    return obj_row(ctx, value).id


def contract_ref(ctx, value: str | None) -> uuid.UUID:
    return contract_row(ctx, value).id
