"""Стороны договора из раздела реквизитов → контрагенты, договор, объект.

Договор оцифрован, но пока стороны остаются только текстом, остальные документы о них «не знают»: АОСР не может
взять заказчика, письмо — адресата, КС-2 — покупателя. Здесь блоки «Заказчик: … / Подрядчик: …» (колонки уже
разложены) разбираются по полям, контрагент заводится или находится по ИНН и привязывается к договору, а заказчик —
ещё и к объекту, если у объекта он не указан.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.brain.card import ORG
from bis.models import ConstructionObject, Contract, Counterparty, Organization

ROLES = {
    "технический заказчик": "developer", "застройщик": "developer", "заказчик": "developer",
    "генеральный подрядчик": "contractor", "генподрядчик": "contractor", "субподрядчик": "subcontractor",
    "подрядчик": "contractor", "исполнитель": "contractor", "поставщик": "supplier", "покупатель": "buyer",
    "проектировщик": "designer",
}
HEAD = re.compile(r"^\s*(" + "|".join(sorted(ROLES, key=len, reverse=True)) + r")\s*:?\s*$", re.IGNORECASE)


def _f(pattern: str, block: str) -> str | None:
    m = re.search(pattern, block, re.IGNORECASE)
    return m.group(1) if m else None


def parse_parties(text: str) -> list[dict[str, Any]]:
    """Блоки сторон из раздела реквизитов (обычно в конце договора)."""
    low = text.lower()
    at = max(low.rfind("реквизит"), low.rfind("адреса сторон"), low.rfind("подписи сторон"))
    tail = text[at:] if at >= 0 else text[-5000:]
    blocks: list[tuple[str, list[str]]] = []
    for line in tail.splitlines():
        m = HEAD.match(line)
        if m:
            blocks.append((m.group(1).lower(), []))
        elif blocks:
            blocks[-1][1].append(line.strip())
    out = []
    for role, lines in blocks:
        block = "\n".join(x for x in lines if x)
        name = ORG.search(block)
        inn = _f(r"ИНН\s*:?\s*(\d{10}|\d{12})\b", block)
        if not (name or inn):
            continue
        addr = next((ln for ln in lines if re.match(r"\d{6},", ln)), None)
        if addr and not re.search(r"\b(ул|пр|д)\.", addr):  # индекс и регион на одной строке, улица — на следующей
            i = lines.index(addr)
            addr = " ".join(lines[i:i + 2])
        pos = next((ln for ln in lines if re.search(r"директор|руководител|управляющ|президент", ln, re.IGNORECASE)), None)
        head = _f(r"([А-ЯЁ]\.\s?[А-ЯЁ]\.\s?[А-ЯЁ][а-яё]+|[А-ЯЁ][а-яё]+\s[А-ЯЁ]\.\s?[А-ЯЁ]\.)", block)
        out.append({"role": role, "kind": ROLES[role], "name": name.group(1) if name else None, "inn": inn,
                    "kpp": _f(r"КПП\s*:?\s*(\d{9})\b", block), "ogrn": _f(r"ОГРНИ?П?\s*:?\s*(\d{13}|\d{15})\b", block),
                    "address": addr, "head_position": pos, "head_name": head})
    return out


def link_contract(db: Session, contract: Contract, text: str) -> dict[str, Any]:
    """Сторону, которая не мы, — в контрагенты и к договору; заказчика — к объекту, если там пусто."""
    org = db.get(Organization, contract.organization_id)
    ours = {(org.inn or "").strip(), (org.name or "").lower()}
    theirs = [p for p in parse_parties(text) if p["inn"] not in ours and (p["name"] or "").lower() not in ours]
    if not theirs:
        return {"parties": 0}
    p = theirs[0]
    cp = db.scalar(select(Counterparty).where(Counterparty.organization_id == contract.organization_id,
                                              Counterparty.inn == p["inn"])) if p["inn"] else None
    if cp is None and p["name"]:
        cp = db.scalar(select(Counterparty).where(Counterparty.organization_id == contract.organization_id,
                                                  Counterparty.name == p["name"]))
    if cp is None:
        cp = Counterparty(organization_id=contract.organization_id, name=p["name"] or f"ИНН {p['inn']}", kind=p["kind"])
        db.add(cp)
    for field in ("inn", "kpp", "ogrn", "address", "head_position", "head_name"):
        if p.get(field) and not getattr(cp, field):
            setattr(cp, field, p[field][:500])
    if cp.kind == "other":
        cp.kind = p["kind"]
    db.flush()
    if contract.counterparty_id is None:
        contract.counterparty_id = cp.id
    linked_object = False
    if contract.object_id and p["kind"] == "developer":
        obj = db.get(ConstructionObject, contract.object_id)
        if obj is not None and obj.developer_id is None:
            obj.developer_id = cp.id
            linked_object = True
    return {"parties": len(theirs), "counterparty": cp.name, "object_developer": linked_object}


def link_all(db: Session, org_id: uuid.UUID) -> int:
    """Договоры без контрагента или объекты без заказчика — досвязать по тексту договора."""
    from bis.brain.build import document_text
    from bis.models import Document

    n = 0
    rows = db.scalars(select(Contract).where(Contract.organization_id == org_id, Contract.document_id.is_not(None))).all()
    for c in rows:
        obj = db.get(ConstructionObject, c.object_id) if c.object_id else None
        if c.counterparty_id is not None and (obj is None or obj.developer_id is not None):
            continue
        doc = db.get(Document, c.document_id)
        if doc is not None and link_contract(db, c, document_text(db, doc, tail=True)).get("parties"):
            n += 1
    return n
