"""Цепочки переписки: что просили, на что ответили, что обещали и чего в архиве ещё нет.

Опирается на карточки писем (сроки, ссылки на акты, документы о качестве, протоколы) и связи «Эрудита»:
- запросы заказчика и ведомств: ответили или нет, срок прошёл или ещё нет;
- наши письма: какие документы в них названы (обещаны или приложены) и каких из них в архиве нет, срок — из письма.
Без модели: только факты из базы, поэтому ассистент не угадывает, что отправлено.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from bis.brain.links import RELATION_TITLES
from bis.models import Document, DocumentLink

# документы, которые письмо может обещать или приложить (ссылки на письма и договоры в эту проверку не входят)
CHECKED = ("quality", "protocol", "scheme", "act")


def _date(v: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


def _ru(iso: str | None) -> str | None:
    d = _date(iso)
    return f"{d:%d.%m.%Y}" if d else None


def _card(doc: Document) -> dict[str, Any]:
    return (doc.data or {}).get("card") or {}


def _label(doc: Document) -> str:
    num = doc.number or _card(doc).get("number") or "б/н"
    return f"№ {num}" + (f" от {doc.date:%d.%m.%Y}" if doc.date else "")


def correspondence(db: Session, org_id: uuid.UUID, object_id: uuid.UUID | None = None,
                   today: dt.date | None = None) -> dict[str, Any]:
    today = today or dt.date.today()
    stmt = select(Document).where(Document.organization_id == org_id, Document.kind.in_(["incoming_letter", "letter"]))
    if object_id:
        stmt = stmt.where(or_(Document.object_id == object_id, Document.object_id.is_(None)))
    letters = db.scalars(stmt.order_by(Document.date.nulls_last(), Document.created_at)).all()
    incoming = {d.id: d for d in letters if d.kind == "incoming_letter"}
    outgoing = {d.id: d for d in letters if d.kind == "letter"}
    links = db.scalars(select(DocumentLink).where(DocumentLink.organization_id == org_id)).all() if letters else []

    # ответ: наше письмо ссылается на их письмо
    answers: dict[uuid.UUID, list[Document]] = {}
    for lk in links:
        if lk.relation == "letter" and lk.to_document_id in incoming and lk.from_document_id in outgoing:
            answers.setdefault(lk.to_document_id, []).append(outgoing[lk.from_document_id])

    requests = []
    for d in incoming.values():
        c = _card(d)
        dl = _date(c.get("deadline"))
        replies = answers.get(d.id, [])
        status = "answered" if replies else "overdue" if dl and dl < today else "waiting"
        requests.append({
            "label": _label(d), "from": c.get("sender") or "", "summary": c.get("summary") or d.title,
            "deadline": dl.isoformat() if dl else None, "status": status,
            "replied_by": [{"label": _label(r), "title": r.title} for r in replies], "document_id": str(d.id),
        })

    # документы, на которые ссылается наше письмо, которых нет в архиве
    by_ref = {(lk.from_document_id, lk.relation, lk.ref_number): lk for lk in links}
    promises = []
    for d in outgoing.values():
        c = _card(d)
        dl = _date(c.get("deadline"))
        missing = []
        for ref in c.get("refs") or []:
            if ref.get("kind") not in CHECKED:
                continue
            lk = by_ref.get((d.id, ref["kind"], ref["number"]))
            if lk is not None and lk.to_document_id is None:
                missing.append({"kind": ref["kind"], "number": ref["number"],
                                "title": RELATION_TITLES.get(ref["kind"], ref["kind"])})
        if not missing:
            continue
        promises.append({
            "label": _label(d), "title": d.title, "to": c.get("recipient") or "", "deadline": dl.isoformat() if dl else None,
            "overdue": bool(dl and dl < today), "missing": missing, "document_id": str(d.id),
        })

    lines = []
    for r in requests:
        if r["status"] == "overdue":
            lines.append(f"Запрос {r['label']} ({r['from'] or 'заказчик'}): ответа нет, срок {_ru(r['deadline'])} прошёл")
        elif r["status"] == "waiting":
            lines.append(f"Запрос {r['label']} ({r['from'] or 'заказчик'}): ответа пока нет" +
                         (f", срок {_ru(r['deadline'])}" if r["deadline"] else ""))
    for p in promises:
        for m in p["missing"]:
            # архив не доказывает, что письмо не ушло: пишем только то, что видно в базе
            when = f", обещали до {_ru(p['deadline'])}" if p["deadline"] else ""
            late = " — срок прошёл" if p["overdue"] else ""
            lines.append(f"В письме {p['label']} назван(а) {m['title']} № {m['number']}: в архиве его нет, "
                         f"отправку архив не подтверждает{when}{late}")
    lines.sort(key=lambda x: "прошёл" not in x)  # просроченное — первым
    return {"today": today.isoformat(), "requests": requests, "promises": promises, "open": lines}
