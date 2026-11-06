"""Сервис документов: нумерация, черновики, утверждение, сборка данных из реестров."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.documents import AOSRData, LetterData, render_aosr, render_letter, validate_aosr
from bis.documents.aosr import REPRESENTATIVE_ROLES, Representative
from bis.documents.common import Party, fmt_date
from bis.documents.templates import render_with_org_template
from bis.models import (
    Appointment,
    ConstructionObject,
    Counter,
    Counterparty,
    Document,
    Organization,
    OrgMemory,
    Person,
    User,
)
from bis.services.audit import audit
from bis.services.storage import Storage

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def next_number(db: Session, org_id: uuid.UUID, key: str) -> int:
    counter = db.scalar(
        select(Counter).where(Counter.organization_id == org_id, Counter.key == key).with_for_update()
    )
    if counter is None:
        counter = Counter(organization_id=org_id, key=key, value=0)
        db.add(counter)
        db.flush()
    counter.value += 1
    db.flush()
    return counter.value


def party_from_org(org: Organization) -> Party:
    return Party(name=org.name, ogrn=org.ogrn, inn=org.inn, kpp=org.kpp, address=org.address,
                 phone=org.phone, email=org.email, sro=org.sro)


def party_from_counterparty(cp: Counterparty) -> Party:
    return Party(name=cp.name, ogrn=cp.ogrn, inn=cp.inn, kpp=cp.kpp, address=cp.address,
                 phone=cp.phone, email=cp.email, sro=cp.sro)


def object_participants(db: Session, org_id: uuid.UUID, object_id: uuid.UUID, on_date: date | None = None) -> dict[str, Any]:
    """Участники объекта и действующие представители по приказам — для заполнения ИД."""
    obj = db.get(ConstructionObject, object_id)
    if obj is None or obj.organization_id != org_id:
        raise LookupError("Объект не найден")
    org = db.get(Organization, org_id)
    on_date = on_date or date.today()
    reps: dict[str, Representative] = {}
    others: list[Representative] = []
    rows = db.scalars(select(Appointment).where(Appointment.organization_id == org_id, Appointment.object_id == object_id))
    for ap in rows:
        if (ap.valid_from and ap.valid_from > on_date) or (ap.valid_to and ap.valid_to < on_date):
            continue
        person: Person = ap.person
        org_name = None
        if person.counterparty_id:
            cp = db.get(Counterparty, person.counterparty_id)
            org_name = cp.name if cp else None
        order = None
        if ap.order_number:
            order = f"приказ № {ap.order_number}" + (f" от {fmt_date(ap.order_date)}" if ap.order_date else "")
        rep = Representative(position=person.position or "", full_name=person.full_name, nrs_id=person.nrs_id,
                             order=order, organization=org_name)
        if ap.role in REPRESENTATIVE_ROLES:
            reps[ap.role] = rep
        else:
            others.append(rep)
    developer = db.get(Counterparty, obj.developer_id) if obj.developer_id else None
    designer = db.get(Counterparty, obj.designer_id) if obj.designer_id else None
    return {
        "object": obj,
        "object_name": obj.name + (f", {obj.address}" if obj.address else ""),
        "builder": party_from_org(org),
        "developer": party_from_counterparty(developer) if developer else None,
        "designer": party_from_counterparty(designer) if designer else None,
        "representatives": reps,
        "other_representatives": others,
    }


def save_document(
    db: Session,
    storage: Storage,
    *,
    org_id: uuid.UUID,
    kind: str,
    title: str,
    data: dict[str, Any],
    file_bytes: bytes,
    file_name: str,
    created_by: str,
    number: str | None = None,
    doc_date: date | None = None,
    object_id: uuid.UUID | None = None,
    contract_id: uuid.UUID | None = None,
    issues: list[dict[str, Any]] | None = None,
    mime: str = DOCX_MIME,
) -> Document:
    key = storage.put(file_bytes, file_name, prefix=f"documents/{org_id}")
    doc = Document(
        organization_id=org_id, kind=kind, title=title, number=number, date=doc_date, status="draft",
        source="generated", object_id=object_id, contract_id=contract_id, file_key=key, file_name=file_name,
        mime=mime, data=data, issues=issues or [], created_by=created_by,
    )
    db.add(doc)
    db.flush()
    actor_type, _, actor = created_by.partition(":")
    audit(db, org_id, actor_type=actor_type or "system", actor=actor or created_by, action="document.create",
          entity_type="document", entity_id=doc.id, details={"kind": kind, "title": title})
    return doc


def create_aosr(
    db: Session, storage: Storage, *, org_id: uuid.UUID, data: AOSRData, created_by: str,
    object_id: uuid.UUID | None = None, contract_id: uuid.UUID | None = None,
) -> Document:
    if not data.number:
        key = f"aosr:{object_id or 'none'}"
        data.number = str(next_number(db, org_id, key))
    data.date = data.date or date.today()
    issues = [i.model_dump() for i in validate_aosr(data)]
    file_bytes = render_with_org_template(db, storage, org_id, "aosr", data) or render_aosr(data)
    title = f"АОСР № {data.number}: {data.works[:150]}"
    return save_document(
        db, storage, org_id=org_id, kind="aosr", title=title, data=data.model_dump(mode="json"),
        file_bytes=file_bytes, file_name=f"АОСР_{data.number}.docx", created_by=created_by,
        number=data.number, doc_date=data.date, object_id=object_id, contract_id=contract_id, issues=issues,
    )


def create_letter(
    db: Session, storage: Storage, *, org_id: uuid.UUID, data: LetterData, created_by: str,
    object_id: uuid.UUID | None = None, contract_id: uuid.UUID | None = None,
) -> Document:
    data.date = data.date or date.today()
    if not data.number:
        seq = next_number(db, org_id, f"letter:{data.date.year}")
        org = db.get(Organization, org_id)
        pattern = (org.settings or {}).get("letter_number_format", "{seq}/{yy}") if org else "{seq}/{yy}"
        data.number = pattern.format(seq=seq, yy=f"{data.date.year % 100:02d}", yyyy=data.date.year)
    file_bytes = render_with_org_template(db, storage, org_id, "letter", data) or render_letter(data)
    title = f"Исх. № {data.number}: {data.subject[:200]}"
    return save_document(
        db, storage, org_id=org_id, kind="letter", title=title, data=data.model_dump(mode="json"),
        file_bytes=file_bytes, file_name=f"Письмо_{data.number.replace('/', '-')}.docx", created_by=created_by,
        number=data.number, doc_date=data.date, object_id=object_id, contract_id=contract_id,
    )


def approve_document(db: Session, doc: Document, user: User) -> Document:
    if doc.status not in ("draft", "on_approval", "rejected"):
        raise ValueError(f"Документ в статусе «{doc.status}» нельзя утвердить")
    blocking = [i for i in doc.issues or [] if i.get("level") == "error"]
    if blocking:
        raise ValueError("Есть ошибки заполнения: " + "; ".join(i["message"] for i in blocking[:5]))
    doc.status = "approved"
    doc.approved_by_id = user.id
    doc.approved_at = datetime.now(UTC)
    doc.reject_reason = None
    audit(db, doc.organization_id, actor_type="user", actor=str(user.id), action="document.approve",
          entity_type="document", entity_id=doc.id)
    return doc


def reject_document(db: Session, doc: Document, user: User, reason: str) -> Document:
    doc.status = "rejected"
    doc.reject_reason = reason
    audit(db, doc.organization_id, actor_type="user", actor=str(user.id), action="document.reject",
          entity_type="document", entity_id=doc.id, details={"reason": reason})
    # Обучение на замечаниях: причина отклонения становится правилом для агента, создавшего черновик.
    if doc.created_by and doc.created_by.startswith("agent:") and reason.strip():
        db.add(OrgMemory(
            organization_id=doc.organization_id, kind="feedback", scope=doc.created_by.split(":", 1)[1],
            content=f"Замечание к документу «{doc.title}»: {reason.strip()}", source=f"document:{doc.id}",
        ))
    return doc
