"""Анализ договоров: реквизиты, обязательства сторон, сроки, неустойки, риски.

Модель извлекает данные по схеме; без модели работает детерминированный разбор по пунктам договора.
Результат пополняет реестр обязательств — их сроки затем контролирует фоновая задача deadlines.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.llm.extract import ExtractResult, extract, find_dates
from bis.llm.gateway import LLMGateway
from bis.models import Contract, ContractObligation, Counterparty, Document, Organization
from bis.rag.parsers import parse_file
from bis.services.audit import audit
from bis.services.storage import Storage

ObligationKind = Literal["works", "payment", "documents", "warranty", "insurance", "other"]


class ExtractedObligation(BaseModel):
    description: str = Field(description="Суть обязательства одним предложением")
    party: Literal["we", "counterparty"] = Field(description="we — наша организация, counterparty — контрагент")
    kind: ObligationKind = "other"
    due_date: dt.date | None = Field(None, description="Конкретная дата срока, если указана")
    due_text: str | None = Field(None, description="Срок словами, если он относительный")
    amount: float | None = None
    penalty: str | None = Field(None, description="Ответственность за нарушение")
    clause: str | None = Field(None, description="Номер пункта договора, например «5.3»")


class ContractExtraction(BaseModel):
    number: str | None = None
    date: dt.date | None = None
    subject: str | None = None
    counterparty_name: str | None = None
    counterparty_inn: str | None = None
    amount: float | None = None
    vat_rate: float | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    advance: str | None = Field(None, description="Условия аванса")
    warranty: str | None = Field(None, description="Гарантийный срок и удержания")
    penalties: list[str] = Field(default_factory=list, description="Условия о неустойках и штрафах")
    obligations: list[ExtractedObligation] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list, description="Условия, невыгодные или рискованные для нашей стороны")


# --- стороны договора -------------------------------------------------------------------------

PARTY_TERMS: dict[tuple[str, str], tuple[list[str], list[str]]] = {
    ("general", "contractor"): (["генподрядчик", "генеральный подрядчик", "подрядчик"], ["заказчик", "застройщик", "технический заказчик"]),
    ("general", "customer"): (["заказчик", "застройщик", "технический заказчик"], ["генподрядчик", "генеральный подрядчик", "подрядчик"]),
    ("subcontract", "customer"): (["генподрядчик", "генеральный подрядчик", "подрядчик", "заказчик"], ["субподрядчик"]),
    ("subcontract", "contractor"): (["субподрядчик"], ["генподрядчик", "генеральный подрядчик", "подрядчик", "заказчик"]),
    ("supply", "customer"): (["покупатель", "заказчик"], ["поставщик"]),
    ("supply", "contractor"): (["поставщик"], ["покупатель", "заказчик"]),
    ("services", "customer"): (["заказчик"], ["исполнитель"]),
    ("services", "contractor"): (["исполнитель"], ["заказчик"]),
    ("design", "customer"): (["заказчик"], ["исполнитель", "подрядчик", "проектировщик"]),
    ("design", "contractor"): (["исполнитель", "подрядчик", "проектировщик"], ["заказчик"]),
}


def party_terms(kind: str, our_role: str) -> tuple[list[str], list[str]]:
    return PARTY_TERMS.get((kind, our_role), (["подрядчик"], ["заказчик"]))


# --- детерминированный разбор -----------------------------------------------------------------

_CLAUSE_LINE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,3}){1,3})\.?\s+(.+)$")
_SECTION_LINE = re.compile(r"^\s*\d{1,2}\.?\s+[А-ЯЁA-Z][А-ЯЁA-Z\s,.\-«»]{3,}$")  # «4. ИСПОЛНИТЕЛЬНАЯ ДОКУМЕНТАЦИЯ»
_OBLIG_RE = re.compile(r"\bобязу[ею]тся|\bобязан[аы]?\b|\bдолж(?:ен|на|ны)\b|не позднее|в течение|\bв срок", re.IGNORECASE)
_PENALTY_RE = re.compile(r"неустойк|пен[яиюе]|штраф", re.IGNORECASE)
_RISK_PATTERNS = [
    (re.compile(r"в\s+одностороннем\s+порядке", re.I), "Право одностороннего изменения/расторжения"),
    (re.compile(r"безакцептн", re.I), "Безакцептное списание/удержание"),
    (re.compile(r"гарантийн\w*\s+удержани", re.I), "Гарантийное удержание из оплаты"),
    (re.compile(r"без\s+(?:ограничени|предела)", re.I), "Ответственность без ограничения суммы"),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*%\s*(?:от[^.]{0,60})?за\s+каждый\s+день", re.I), None),  # проверка ставки ниже
]
_KIND_RULES: list[tuple[str, str]] = [
    ("insurance", r"страхов"),
    ("warranty", r"гарант"),
    ("payment", r"оплат|оплач|аванс|перечисл|платеж|платёж"),
    ("documents", r"документ|исполнительн|\bакт|кс-2|кс-3|\bсч[её]т"),
    ("works", r"выполн|заверш|работ|строительств|монтаж|устройств|возвед|поставк|передат"),
]


def _kind(text: str) -> str:
    low = text.lower()
    for kind, pattern in _KIND_RULES:
        if re.search(pattern, low):
            return kind
    return "other"


def _money(text: str) -> float | None:
    m = re.search(r"(\d{1,3}(?:[\s ]\d{3})+|\d{4,})(?:[.,](\d{2}))?\s*(?:\(.*?\)\s*)?(?:руб|₽)", text)
    if not m:
        return None
    try:
        return float(Decimal(re.sub(r"[\s ]", "", m.group(1)) + "." + (m.group(2) or "00")))
    except InvalidOperation:
        return None


def _clauses(text: str) -> list[tuple[str | None, str]]:
    """Разбивает договор на пункты: [(номер, текст)]."""
    out: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in text.splitlines():
        m = _CLAUSE_LINE.match(line)
        if m and len(m.group(2)) > 3:
            out.append((m.group(1), [m.group(2).strip()]))
        elif _SECTION_LINE.match(line):
            out.append((None, []))  # заголовок раздела завершает предыдущий пункт
        elif line.strip():
            out[-1][1].append(line.strip())
    return [(n, " ".join(lines)) for n, lines in out if lines]


def heuristic_extract(text: str, *, kind: str, our_role: str, our_inn: str | None = None) -> ContractExtraction:
    ours, theirs = party_terms(kind, our_role)
    head = text[:1500]
    res = ContractExtraction()
    m = re.search(r"договор[^\n№]{0,60}№\s*([\w\-/.]+)", head, re.IGNORECASE)
    if m:
        res.number = m.group(1).rstrip(".")
    dates = find_dates(head)
    if dates:
        res.date = dates[0][1]
    inns = [x for x in re.findall(r"ИНН\s*:?\s*(\d{10}|\d{12})", text) if x != our_inn]
    res.counterparty_inn = inns[0] if inns else None
    vat = re.search(r"НДС[^\d%]{0,40}(\d{1,2})\s*%", text)
    res.vat_rate = float(vat.group(1)) if vat else None

    for num, body in _clauses(text):
        low = body.lower()
        if res.amount is None and re.search(r"цена|стоимость", low):
            res.amount = _money(body)
        if res.subject is None and "предмет" in low[:40] and num and num.startswith("1."):
            res.subject = body[:500]
        if _PENALTY_RE.search(body):
            res.penalties.append((f"п. {num}: " if num else "") + body[:400])
        for pattern, label in _RISK_PATTERNS:
            mm = pattern.search(body)
            if not mm:
                continue
            if label is None:
                rate = float(mm.group(1).replace(",", "."))
                if rate >= 0.1:
                    res.risks.append(f"п. {num}: высокая ставка неустойки {mm.group(1)}% в день")
            else:
                res.risks.append(f"п. {num}: {label}")
        if not _OBLIG_RE.search(body) or _PENALTY_RE.search(body):
            continue
        first = low[:120]
        pos_ours = _first_pos(first, ours)
        pos_theirs = _first_pos(first, theirs)
        if pos_ours < 0 and pos_theirs < 0:
            continue
        party = "we" if pos_theirs < 0 or (0 <= pos_ours < pos_theirs) else "counterparty"
        pos = pos_ours if party == "we" else pos_theirs
        body_dates = find_dates(body)
        res.obligations.append(ExtractedObligation(
            description=body[:400], party=party, kind=_kind(low[pos:pos + 160]),
            due_date=body_dates[-1][1] if body_dates else None,
            due_text=None if body_dates else _due_text(body),
            amount=_money(body), clause=num,
            penalty=None,
        ))
    _attach_penalties(res, ours, theirs)
    works_dates = [o.due_date for o in res.obligations if o.party == "we" and o.kind == "works" and o.due_date]
    if works_dates:
        res.end_date = max(works_dates)  # срок окончания работ — самый поздний срок по работам
    return res


def _attach_penalties(res: ContractExtraction, ours: list[str], theirs: list[str]) -> None:
    """Связывает условия о неустойке с обязательствами той же стороны и того же вида."""
    for text in res.penalties:
        low = text.lower()
        po, pt = _first_pos(low, ours), _first_pos(low, theirs)
        if po < 0 and pt < 0:
            continue
        party = "we" if pt < 0 or (0 <= po < pt) else "counterparty"
        kind = "payment" if "оплат" in low else "works" if re.search(r"выполнени|работ", low) else None
        for ob in res.obligations:
            if ob.party == party and (kind is None or ob.kind == kind) and not ob.penalty:
                ob.penalty = text[:300]


def _first_pos(text: str, terms: list[str]) -> int:
    """Позиция первого упоминания стороны (целым словом: «подрядчик» не находится внутри «субподрядчик»)."""
    found = [m.start() for t in terms if (m := re.search(rf"\b{t}", text))]
    return min(found) if found else -1


def _due_text(body: str) -> str | None:
    m = re.search(r"(в течение[^.;]{3,120}|не позднее[^.;]{3,120}|в срок[^.;]{3,120})", body, re.IGNORECASE)
    return m.group(1).strip() if m else None


# --- сервис -----------------------------------------------------------------------------------

INSTRUCTIONS = """Ты юрист строительной компании. Извлеки из договора реквизиты, обязательства сторон, сроки,
неустойки и риски. Наша организация: «{org}». Вид договора: {kind}. Наша роль: {role}.
Наша сторона в тексте называется: {ours}. Контрагент: {theirs}.
- obligations: все обязательства с конкретным действием и сроком (оплата, выполнение работ, передача
  документов, гарантия, страхование). party=we — если обязанность нашей стороны.
- Если срок относительный («в течение 10 рабочих дней после подписания КС-2») — заполни due_text, due_date=null.
- risks: условия, опасные для нашей стороны (односторонние права контрагента, высокие неустойки, удержания,
  безакцептное списание, короткие сроки приёмки, отсутствие ограничения ответственности)."""

KIND_TITLES = {"general": "генподряд", "subcontract": "субподряд", "supply": "поставка", "services": "услуги",
               "design": "проектирование"}


def contract_text(db: Session, storage: Storage, contract: Contract) -> str:
    if contract.document_id is None:
        raise ValueError("К договору не прикреплён файл — загрузите текст договора")
    doc = db.get(Document, contract.document_id)
    if doc is None or not doc.file_key:
        raise ValueError("Файл договора не найден")
    return parse_file(storage.get(doc.file_key), doc.file_name or "contract.pdf", doc.mime).text


def analyze_contract(db: Session, storage: Storage, gateway: LLMGateway | None, contract: Contract,
                     *, actor: str, text: str | None = None) -> tuple[ContractExtraction, ExtractResult, int]:
    """Анализирует договор и добавляет обязательства в реестр. Возвращает (данные, сведения о запуске, новых обязательств)."""
    text = text or contract_text(db, storage, contract)
    org = db.get(Organization, contract.organization_id)
    ours, theirs = party_terms(contract.kind, contract.our_role)
    result = extract(
        gateway, ContractExtraction, text,
        instructions=INSTRUCTIONS.format(org=org.name, kind=KIND_TITLES.get(contract.kind, contract.kind),
                                         role="подрядчик/исполнитель" if contract.our_role == "contractor" else "заказчик",
                                         ours=", ".join(ours), theirs=", ".join(theirs)),
        task="analysis", sensitivity="internal",
        fallback=lambda t: heuristic_extract(t, kind=contract.kind, our_role=contract.our_role, our_inn=org.inn),
    )
    data = result.data or ContractExtraction()
    _fill_contract(db, contract, data)
    added = _upsert_obligations(db, contract, data, actor=actor if not result.used_fallback else f"{actor}/правила")
    contract.extra = {**(contract.extra or {}), "analysis": {
        "analyzed_at": dt.datetime.now(dt.UTC).isoformat(), "provider": result.provider,
        "used_fallback": result.used_fallback, "advance": data.advance, "warranty": data.warranty,
        "penalties": data.penalties[:30], "risks": data.risks[:30], "errors": result.errors[:5],
    }}
    audit(db, contract.organization_id, actor_type="agent" if actor.startswith("agent") else "user", actor=actor,
          action="contract.analyze", entity_type="contract", entity_id=contract.id,
          details={"obligations_added": added, "risks": len(data.risks), "fallback": result.used_fallback})
    db.flush()
    return data, result, added


def _fill_contract(db: Session, contract: Contract, data: ContractExtraction) -> None:
    for field in ("subject", "date", "start_date", "end_date"):
        if getattr(contract, field) in (None, "") and getattr(data, field):
            setattr(contract, field, getattr(data, field))
    if contract.amount is None and data.amount:
        contract.amount = Decimal(str(data.amount))
    if contract.vat_rate is None and data.vat_rate is not None:
        contract.vat_rate = Decimal(str(data.vat_rate))
    if contract.counterparty_id is None and data.counterparty_inn:
        cp = db.scalar(select(Counterparty).where(Counterparty.organization_id == contract.organization_id,
                                                  Counterparty.inn == data.counterparty_inn))
        if cp is None and data.counterparty_name:
            cp = Counterparty(organization_id=contract.organization_id, name=data.counterparty_name,
                              inn=data.counterparty_inn, kind="other")
            db.add(cp)
            db.flush()
        if cp:
            contract.counterparty_id = cp.id


def _fingerprint(text: str) -> str:
    return hashlib.sha1(re.sub(r"\W+", " ", text.lower()).strip()[:300].encode()).hexdigest()


def _upsert_obligations(db: Session, contract: Contract, data: ContractExtraction, *, actor: str) -> int:
    existing = {_fingerprint(o.description) for o in db.scalars(
        select(ContractObligation).where(ContractObligation.contract_id == contract.id))}
    added = 0
    for ob in data.obligations:
        fp = _fingerprint(ob.description)
        if fp in existing:
            continue
        existing.add(fp)
        db.add(ContractObligation(
            organization_id=contract.organization_id, contract_id=contract.id, description=ob.description,
            party=ob.party, kind=ob.kind, due_date=ob.due_date, due_text=ob.due_text,
            amount=Decimal(str(ob.amount)) if ob.amount is not None else None, penalty=ob.penalty,
            source_clause=f"п. {ob.clause}" if ob.clause and not ob.clause.startswith("п") else ob.clause,
            source_text=ob.description, responsible_id=contract.responsible_id if ob.party == "we" else None,
            extracted_by=actor, status="open",
        ))
        added += 1
    return added


# --- неустойка --------------------------------------------------------------------------------


class PenaltyCalc(BaseModel):
    base_amount: float
    days: int
    rate_percent_per_day: float | None = None
    key_rate_percent: float | None = None
    key_rate_fraction: str | None = None  # «1/300», «1/150»
    cap_percent: float | None = None
    penalty: float = 0.0
    formula: str = ""


def calc_penalty(base_amount: float, days: int, *, rate_percent_per_day: float | None = None,
                 key_rate_percent: float | None = None, key_rate_fraction: str | None = None,
                 cap_percent: float | None = None) -> PenaltyCalc:
    """Неустойка: процент в день или доля ключевой ставки ЦБ в день, с ограничением в % от суммы."""
    if days < 0 or base_amount < 0:
        raise ValueError("Сумма и количество дней не могут быть отрицательными")
    if rate_percent_per_day is not None:
        daily = Decimal(str(rate_percent_per_day)) / 100
        formula = f"{base_amount:,.2f} × {rate_percent_per_day}% × {days} дн."
    elif key_rate_percent is not None and key_rate_fraction:
        num, den = (Decimal(x) for x in key_rate_fraction.split("/"))
        daily = Decimal(str(key_rate_percent)) / 100 * num / den
        formula = f"{base_amount:,.2f} × {key_rate_fraction} × {key_rate_percent}% × {days} дн."
    else:
        raise ValueError("Укажите ставку в % за день или ключевую ставку и долю (например, 1/300)")
    value = Decimal(str(base_amount)) * daily * days
    if cap_percent is not None:
        cap = Decimal(str(base_amount)) * Decimal(str(cap_percent)) / 100
        if value > cap:
            value = cap
            formula += f", ограничено {cap_percent}% суммы"
    return PenaltyCalc(base_amount=base_amount, days=days, rate_percent_per_day=rate_percent_per_day,
                       key_rate_percent=key_rate_percent, key_rate_fraction=key_rate_fraction, cap_percent=cap_percent,
                       penalty=float(value.quantize(Decimal("0.01"))), formula=formula.replace(",", " "))


def attach_contract_file(db: Session, storage: Storage, contract: Contract, data: bytes, filename: str,
                         mime: str | None, actor: str) -> Document:
    from bis.services.documents import save_document

    doc = save_document(db, storage, org_id=contract.organization_id, kind="contract",
                        title=f"Договор № {contract.number}", data={}, file_bytes=data, file_name=filename,
                        created_by=actor, number=contract.number, doc_date=contract.date, object_id=contract.object_id,
                        contract_id=contract.id, mime=mime or "application/octet-stream")
    doc.source, doc.status = "uploaded", "registered"
    contract.document_id = doc.id
    db.flush()
    return doc

