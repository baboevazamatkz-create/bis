"""Связи документов: на что ссылается документ и что ссылается на него.

АОСР → документы о качестве, протоколы, схемы; письмо → письмо, на которое отвечает, и договор; любой документ →
упомянутые акты. Ссылка на документ, которого ещё нет в архиве, хранится с номером и находит его, когда он появится.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from bis.brain.card import Card
from bis.models import Contract, Document, DocumentLink

RELATION_TITLES = {
    "quality": "документ о качестве", "protocol": "протокол испытаний", "scheme": "исполнительная схема",
    "letter": "ответ на письмо", "contract": "договор", "act": "акт", "rd": "лист РД", "revision": "прежнее изменение",
}
TARGET_KINDS = {
    "quality": ["certificate", "supply_doc", "other"], "protocol": ["test_protocol", "other"], "scheme": ["scheme", "other"],
    "letter": ["letter", "incoming_letter"], "contract": ["contract"], "act": ["aosr", "aook"],
}


def _exact(num: str) -> re.Pattern:
    return re.compile(rf"(?<![\w]){re.escape(num)}(?![\w])", re.IGNORECASE)


def resolve(db: Session, org_id: uuid.UUID, relation: str, number: str, exclude: uuid.UUID | None = None,
            object_id: uuid.UUID | None = None, ref_date: str | None = None) -> Document | None:
    """Документ архива по номеру ссылки: номер документа, название, номер в карточке."""
    from bis.services.library_access import SourceContext, document_predicate, strict_enabled

    strict = strict_enabled(db, org_id)
    if relation == "contract":
        query = select(Contract).where(Contract.organization_id == org_id, Contract.number.ilike(number))
        if strict:
            query = query.where(Contract.object_id == object_id) if object_id else query.where(False)
        contracts = db.scalars(query).all()
        if object_id:
            contracts = [c for c in contracts if c.object_id == object_id or (not strict and c.object_id is None)]
        if ref_date:
            contracts = [c for c in contracts if c.date and c.date.isoformat() == ref_date]
        if len(contracts) == 1 and contracts[0].document_id:
            return db.get(Document, contracts[0].document_id)
    exact = _exact(number)
    stmt = select(Document).where(Document.organization_id == org_id,
                                  or_(Document.number.ilike(f"%{number}%"), Document.title.ilike(f"%{number}%"),
                                      Document.data["card"]["number"].astext == number))
    if strict:
        stmt = stmt.where(document_predicate(SourceContext(org_id, object_id, True)))
    if exclude:
        stmt = stmt.where(Document.id != exclude)
    if object_id:
        stmt = stmt.where(or_(Document.object_id == object_id, Document.object_id.is_(None)))
    if ref_date:
        import datetime as dt

        stmt = stmt.where(Document.date == dt.date.fromisoformat(ref_date))
    kinds = TARGET_KINDS.get(relation)
    cands = [d for d in db.scalars(stmt).all()
             if exact.search(f"{d.number or ''} {d.title.replace('_', ' ')} {((d.data or {}).get('card') or {}).get('number') or ''}")]
    if kinds:
        preferred = [d for d in cands if d.kind in kinds]
        cands = preferred if relation == "act" else (preferred or [d for d in cands if d.kind not in ("aosr", "aook")])
    if len(cands) == 1:
        return cands[0]
    if cands:
        return None  # the same number in two documents is not evidence for choosing either
    if relation in ("quality", "protocol", "scheme"):  # номер внутри текста сертификата/протокола
        from bis.services.id_check import Ref, _find

        return _find(db, org_id, Ref(kind=relation, title="", number=number), exclude or uuid.uuid4(), object_id=object_id)
    return None


def rebuild(db: Session, doc: Document, card: Card) -> list[DocumentLink]:
    """Пересобрать исходящие связи документа по его карточке, затем найти тех, кто ждал этот документ."""
    db.execute(delete(DocumentLink).where(DocumentLink.from_document_id == doc.id, DocumentLink.relation != "package"))
    out = []
    for ref in card.refs:
        target = resolve(db, doc.organization_id, ref.kind, ref.number, exclude=doc.id,
                         object_id=doc.object_id, ref_date=ref.date)
        link = DocumentLink(organization_id=doc.organization_id, from_document_id=doc.id,
                            to_document_id=target.id if target else None, relation=ref.kind, ref_number=ref.number[:200],
                            label=(ref.label or RELATION_TITLES.get(ref.kind, ref.kind))[:300])
        db.add(link)
        out.append(link)
    db.flush()
    resolve_pending(db, doc)
    return out


def resolve_pending(db: Session, doc: Document) -> int:
    """Новый документ мог быть тем, на который раньше сослались по номеру (сертификат загрузили после акта)."""
    card = (doc.data or {}).get("card") or {}
    hay = f"{doc.number or ''} {doc.title.replace('_', ' ')} {card.get('number') or ''}"
    pending = db.scalars(select(DocumentLink).where(DocumentLink.organization_id == doc.organization_id,
                                                    DocumentLink.from_document_id != doc.id)).all()
    n = 0
    for link in pending:
        kinds = TARGET_KINDS.get(link.relation)
        if link.ref_number and _exact(link.ref_number).search(hay) and (not kinds or doc.kind in kinds or doc.kind == "other"):
            source = db.get(Document, link.from_document_id)
            refs = ((source.data or {}).get("card") or {}).get("refs", []) if source else []
            ref_date = next((r.get("date") for r in refs if r.get("kind") == link.relation and r.get("number") == link.ref_number), None)
            target = resolve(db, doc.organization_id, link.relation, link.ref_number, exclude=link.from_document_id,
                             object_id=source.object_id if source else None, ref_date=ref_date)
            target_id = target.id if target else None
            if link.to_document_id != target_id:
                link.to_document_id = target_id
                n += int(target_id is not None)
    return n


def neighbours(db: Session, doc: Document) -> dict[str, list[dict[str, Any]]]:
    """Связи документа в обе стороны — для ответа ассистента и карточки в интерфейсе."""
    def ref(d: Document | None) -> dict[str, Any] | None:
        if d is None:
            return None
        return {"id": str(d.id), "title": d.title, "kind": d.kind, "date": d.date.isoformat() if d.date else None}

    outgoing = [{"relation": link.relation, "label": link.label, "number": link.ref_number,
                 "found": link.to_document_id is not None,
                 "document": ref(db.get(Document, link.to_document_id) if link.to_document_id else None)}
                for link in db.scalars(select(DocumentLink).where(DocumentLink.from_document_id == doc.id))]
    incoming = [{"relation": link.relation, "label": link.label, "number": link.ref_number,
                 "document": ref(db.get(Document, link.from_document_id))}
                for link in db.scalars(select(DocumentLink).where(DocumentLink.to_document_id == doc.id))]
    return {"references": outgoing, "referenced_by": incoming}
