"""Обработка входящей корреспонденции (агент «Секретарь»).

Входящее письмо → регистрация (вх. №) → извлечение: отправитель, исх. №, тема, суть, категория,
срочность, срок ответа, конкретные просьбы/требования → связь с контрагентом и договором →
поручения ответственным по правилам маршрутизации → уведомления.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.llm.extract import extract, find_dates
from bis.llm.gateway import LLMGateway
from bis.models import Contract, Counterparty, Document, Organization, Person, Task, User
from bis.rag.parsers import parse_file
from bis.services.audit import audit
from bis.services.documents import next_number
from bis.services.notify import notify
from bis.services.storage import Storage

Category = Literal["request", "claim", "instruction", "notification", "documents", "invoice", "other"]
CATEGORY_TITLES = {"request": "Запрос", "claim": "Претензия", "instruction": "Предписание", "notification": "Уведомление",
                   "documents": "Передача документов", "invoice": "Счёт / оплата", "other": "Прочее"}
Role = Literal["pto", "lawyer", "supply", "foreman", "manager", "secretary"]


class LetterRequest(BaseModel):
    text: str = Field(description="Что конкретно просят или требуют сделать")
    due_date: dt.date | None = None
    role: Role = Field("manager", description="Кто должен исполнять: pto, lawyer, supply, foreman, manager, secretary")


class IncomingAnalysis(BaseModel):
    sender_org: str | None = None
    sender_inn: str | None = None
    sender_person: str | None = Field(None, description="Подписант письма")
    their_number: str | None = Field(None, description="Исходящий номер отправителя")
    their_date: dt.date | None = None
    subject: str | None = None
    summary: str = Field("", description="Суть письма в 1–3 предложениях")
    category: Category = "other"
    urgency: Literal["low", "normal", "high"] = "normal"
    response_required: bool = False
    response_due: dt.date | None = None
    requests: list[LetterRequest] = Field(default_factory=list)
    contract_number: str | None = Field(None, description="Номер договора, если упоминается")
    reply_to_our_number: str | None = Field(None, description="Если это ответ на наше письмо — наш исх. №")


# --- правила без модели -------------------------------------------------------------------------

_CATEGORY_RULES: list[tuple[str, str]] = [
    ("claim", r"претензи|требуем\s+(?:уплат|возмест|оплат)|неустойк"),
    ("instruction", r"предписани|предписыва|устранить\s+нарушени"),
    ("invoice", r"\bсч[её]т\b|счёт на оплату|к оплате|задолженност"),
    ("documents", r"направляем|направляю|препровожда|во исполнение"),
    ("request", r"прошу|просим|просьба|необходимо предоставить|предоставьте"),
    ("notification", r"уведомля|сообщаем|информируем|ставим в известность"),
]
_ROLE_RULES: list[tuple[str, str]] = [
    ("pto", r"исполнительн|\bакт\w*\s+(?:освидетельств|скрыт)|аоср|\bид\b|проектн|чертеж|чертёж|схем"),
    ("lawyer", r"претензи|договор|неустойк|штраф|суд"),
    ("supply", r"материал|поставк|арматур|бетон\b|сертификат|паспорт"),
    ("foreman", r"работ[ыа]? на объекте|площадк|график производства|бригад|охран\w+ труда|техник\w+ безопасност"),
    ("manager", r"оплат|сч[её]т|задолженност|финанс"),
]
_S = r"(?:[^.!?\n]|\.(?=\S))*"  # часть предложения; точка внутри даты/номера не завершает его
_REQUEST_SENT = re.compile(_S + r"(?:прошу|просим|необходимо|требуем|предоставьте|обеспечьте|устраните)" + _S + r"[.!?]?",
                           re.IGNORECASE)
_ORG_LINE = re.compile(r"^\s*((?:ООО|АО|ПАО|ЗАО|ОАО|ИП|ФГУП|ГУП|МУП|ГКУ|ГБУ)\s+[«\"][^\n]{2,150})$", re.MULTILINE)
_NUMBER_RE = re.compile(r"(?:исх\.?\s*)?№\s*([\w\-/.]+)\s+от\s+(\d{1,2}[./]\d{1,2}[./]\d{2,4})", re.IGNORECASE)
_RELATIVE_RE = re.compile(r"в\s+течение\s+(\d{1,3})\s+(рабоч\w+\s+)?(?:дн|календарн)", re.IGNORECASE)
_CONTRACT_RE = re.compile(r"договор\w*[^№\n]{0,40}№\s*([\w\-/.]+)", re.IGNORECASE)


def add_business_days(start: dt.date, days: int) -> dt.date:
    d = start
    while days > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def _role(text: str) -> str:
    low = text.lower()
    for role, pattern in _ROLE_RULES:
        if re.search(pattern, low):
            return role
    return "manager"


def heuristic_incoming(text: str, *, received: dt.date, sender: str | None = None,
                       subject: str | None = None) -> IncomingAnalysis:
    low = text.lower()
    if not sender:
        om = _ORG_LINE.search(text[:600])
        sender = om.group(1).strip() if om else None
    a = IncomingAnalysis(sender_org=sender, subject=subject)
    m = _NUMBER_RE.search(text)
    if m:
        a.their_number = m.group(1).rstrip(".,")
        dates = find_dates(m.group(0))
        a.their_date = dates[0][1] if dates else None
    if not a.subject:
        sm = re.search(r"^\s*(О[б]?\s+[^\n]{5,200})$", text, re.MULTILINE)
        a.subject = sm.group(1).strip() if sm else None
    inn = re.search(r"ИНН\s*:?\s*(\d{10}|\d{12})", text)
    a.sender_inn = inn.group(1) if inn else None
    for cat, pattern in _CATEGORY_RULES:
        if re.search(pattern, low):
            a.category = cat
            break
    cm = _CONTRACT_RE.search(text)
    a.contract_number = cm.group(1).rstrip(".,") if cm else None
    base = a.their_date or received
    rel = _RELATIVE_RE.search(text)
    if rel:
        n = int(rel.group(1))
        a.response_due = add_business_days(base, n) if rel.group(2) else base + dt.timedelta(days=n)
    else:
        due = [d for pos, d in find_dates(text) if d > base and re.search(r"(?:до|не позднее|срок)\s*\S*$", text[max(0, pos - 25):pos])]
        a.response_due = due[0] if due else None
    for sent in _REQUEST_SENT.findall(text):
        sent = " ".join(sent.split())
        if len(sent) < 15:
            continue
        sent_dates = [d for _, d in find_dates(sent) if d >= base]
        a.requests.append(LetterRequest(text=sent[:400], due_date=sent_dates[0] if sent_dates else a.response_due,
                                        role=_role(sent)))
    a.response_required = a.category in ("request", "claim", "instruction") or bool(a.requests)
    a.urgency = "high" if a.category in ("claim", "instruction") or re.search(r"срочно|незамедлительно", low) else "normal"
    body = re.sub(r"\s+", " ", text).strip()
    a.summary = (a.requests[0].text if a.requests else body[:300])
    return a


INSTRUCTIONS = """Ты секретарь-референт строительной компании «{org}». Разбери входящее письмо.
- sender_org — организация-отправитель (не наша), their_number/their_date — исходящий номер и дата отправителя.
- requests — каждое конкретное требование/просьба отдельно, с ролью исполнителя:
  pto — исполнительная и проектная документация; lawyer — договоры, претензии, неустойки; supply — материалы и
  поставки; foreman — работы на площадке, график, охрана труда; manager — оплата, финансы, прочее.
- response_due — срок ответа; если указан относительный срок («в течение 5 рабочих дней»), посчитай от даты письма
  ({today}) с учётом выходных.
- category: request, claim, instruction, notification, documents, invoice, other."""


# --- маршрутизация ------------------------------------------------------------------------------


def assignee_for_role(db: Session, org: Organization, role: str, contract: Contract | None) -> tuple[Person | None, User | None]:
    """Исполнитель по роли: правила организации (settings.routing) → пользователь с ролью → ответственный по договору."""
    routing = (org.settings or {}).get("routing", {})
    email = routing.get(role)
    user = None
    if email:
        user = db.scalar(select(User).where(User.organization_id == org.id, User.email == email.lower(), User.is_active))
    if user is None:
        db_role = {"secretary": "secretary"}.get(role, role)
        user = db.scalar(select(User).where(User.organization_id == org.id, User.role == db_role,
                                            User.is_active.is_(True)).order_by(User.created_at))
    if user is not None:
        person = db.scalar(select(Person).where(Person.organization_id == org.id, Person.user_id == user.id))
        return person, user
    if contract and contract.responsible_id:
        person = db.get(Person, contract.responsible_id)
        return person, db.get(User, person.user_id) if person and person.user_id else None
    return None, None


def process_incoming(db: Session, storage: Storage, gateway: LLMGateway | None, doc: Document, *, actor: str,
                     text: str | None = None, sender_hint: str | None = None,
                     subject_hint: str | None = None) -> IncomingAnalysis:
    org = db.get(Organization, doc.organization_id)
    if text is None:
        parsed = parse_file(storage.get(doc.file_key), doc.file_name or "letter.txt", doc.mime)
        text = parsed.text
        email = parsed.meta.get("email") or {}
        sender_hint = sender_hint or email.get("From")
        subject_hint = subject_hint or email.get("Subject")
    received = (doc.created_at.date() if doc.created_at else None) or dt.date.today()
    result = extract(
        gateway, IncomingAnalysis, text,
        instructions=INSTRUCTIONS.format(org=org.name, today=received.strftime("%d.%m.%Y")),
        task="extract", window_chars=30000,
        fallback=lambda t: heuristic_incoming(t, received=received, sender=sender_hint, subject=subject_hint),
    )
    a = result.data or IncomingAnalysis()

    # регистрация
    seq = next_number(db, org.id, f"incoming:{received.year}")
    doc.number = doc.number or f"вх-{seq}"
    doc.date = doc.date or received
    doc.kind = "incoming_letter"
    doc.status = "registered"
    label = CATEGORY_TITLES[a.category]
    doc.title = f"Вх. № {doc.number}: {label.lower()} — {(a.subject or a.summary or doc.title)[:200]}"

    cp = None
    if a.sender_inn:
        cp = db.scalar(select(Counterparty).where(Counterparty.organization_id == org.id, Counterparty.inn == a.sender_inn))
    if cp is None and a.sender_org:
        cp = db.scalar(select(Counterparty).where(Counterparty.organization_id == org.id,
                                                  Counterparty.name.ilike(f"%{_core_name(a.sender_org)}%")))
    contract = None
    if a.contract_number:
        contract = db.scalar(select(Contract).where(Contract.organization_id == org.id, Contract.number == a.contract_number))
    if contract is None and cp is not None:
        contract = db.scalar(select(Contract).where(Contract.organization_id == org.id, Contract.counterparty_id == cp.id,
                                                    Contract.status == "active").order_by(Contract.date.desc().nulls_last()))
    if contract:
        doc.contract_id = doc.contract_id or contract.id
        doc.object_id = doc.object_id or contract.object_id

    # поручения
    tasks: list[Task] = []
    for req in a.requests[:10]:
        person, user = assignee_for_role(db, org, req.role, contract)
        tasks.append(_task(db, doc, title=req.text, due=req.due_date or a.response_due, person=person, user=user,
                           priority="high" if a.urgency == "high" else "normal", actor=actor, contract=contract))
    if a.response_required:
        person, user = assignee_for_role(db, org, "secretary", contract)
        due = a.response_due or add_business_days(received, 10)
        tasks.append(_task(db, doc, title=f"Подготовить ответ на вх. № {doc.number}"
                           + (f" (исх. № {a.their_number} {cp.name if cp else a.sender_org or ''})".rstrip() if a.their_number else ""),
                           due=due, person=person, user=user, priority="high" if a.urgency == "high" else "normal",
                           actor=actor, contract=contract))
    db.flush()
    doc.data = {**(doc.data or {}), "analysis": a.model_dump(mode="json"), "counterparty_id": str(cp.id) if cp else None,
                "tasks": [str(t.id) for t in tasks], "method": "rules" if result.used_fallback else result.provider}
    audit(db, org.id, actor_type="agent" if actor.startswith("agent") else "system", actor=actor,
          action="incoming.process", entity_type="document", entity_id=doc.id,
          details={"category": a.category, "tasks": len(tasks), "fallback": result.used_fallback})
    for t in tasks:
        if t.assignee_user_id:
            notify(db, org.id, [db.get(User, t.assignee_user_id)], title=f"Поручение по вх. № {doc.number}", body=t.title,
                   level="warning" if a.urgency == "high" else "info", entity_type="task", entity_id=str(t.id),
                   key=f"task:{t.id}:new")
    return a


def _core_name(name: str) -> str:
    """«ООО "Ромашка"» → «Ромашка» для поиска контрагента по названию."""
    core = re.sub(r"^(ООО|АО|ПАО|ЗАО|ОАО|ИП|ФГУП|ГУП|МУП)\s+", "", name.strip(), flags=re.IGNORECASE)
    return core.strip(" «»\"'")[:60] or name


def _task(db: Session, doc: Document, *, title: str, due: dt.date | None, person: Person | None, user: User | None,
          priority: str, actor: str, contract: Contract | None) -> Task:
    t = Task(organization_id=doc.organization_id, title=title[:1000], due_date=due,
             assignee_person_id=person.id if person else None, assignee_user_id=user.id if user else None,
             priority=priority, source_type="letter", source_id=str(doc.id), object_id=doc.object_id,
             contract_id=contract.id if contract else None, created_by=actor)
    db.add(t)
    return t
