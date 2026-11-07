"""Проверка исполнительной документации по архиву (как «недостающие документы» у Autodesk AutoSpecs и сервисов ИД).

Из текста каждого АОСР/АООК берутся ссылки: документы о качестве (сертификаты, паспорта, декларации), протоколы
испытаний, исполнительные схемы, шифр рабочей документации, даты работ. Для каждой ссылки ищем документ в архиве
организации — по номеру в названии, номере или тексте. Итог: какие акты полные, чего не хватает и где.
Без модели: правила и поиск по базе — быстро и проверяемо.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from bis.models import Document, KnowledgeChunk, KnowledgeSource

REF_KINDS = {
    "quality": ("документ о качестве", r"(?:документ\w*\s+о\s+качеств\w*|паспорт\w*(?:\s+качеств\w*)?|сертификат\w*"
                                        r"(?:\s+соответстви\w*)?|деклараци\w*(?:\s+о\s+соответствии)?)"),
    "protocol": ("протокол испытаний", r"протокол\w*(?:\s+(?:испытани\w*|лабораторн\w*|контрол\w*))?(?:\s+образц\w*)?"),
    "scheme": ("исполнительная схема", r"(?:исполнительн\w+\s+(?:геодезическ\w+\s+)?)?схем\w*"),
}
NUM = r"\s*№\s*([A-Za-zА-Яа-я]{0,4}[-–]?\d[\w\-/–.]*)"
WITH_SCHEME = re.compile(r"армирован|бетонирован|опалуб|фундамент|монтаж|кладк|сва[йи]|колонн|перекрыт|стен|разбивк|"
                         r"котлован|обратн\w+\s+засыпк|трубопровод|сет[еи]", re.IGNORECASE)
TARGET_KINDS = {"quality": ["certificate", "supply_doc", "other"], "protocol": ["test_protocol", "other"],
                "scheme": ["scheme", "other"]}


@dataclass
class Ref:
    kind: str
    title: str
    number: str
    found: bool = False
    document_id: str | None = None
    document_title: str | None = None


@dataclass
class ActCheck:
    document_id: str
    title: str
    date: str | None
    works: str
    refs: list[Ref] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.issues and all(r.found for r in self.refs)


def _text(db: Session, doc: Document) -> str:
    parts = db.scalars(select(KnowledgeChunk.content).join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
                       .where(KnowledgeSource.document_id == doc.id).order_by(KnowledgeChunk.ord).limit(60)).all()
    return "\n".join(parts)


def references(text: str) -> list[Ref]:
    out: list[Ref] = []
    seen: set[tuple[str, str]] = set()
    for kind, (title, pattern) in REF_KINDS.items():
        for m in re.finditer(pattern + NUM, text, re.IGNORECASE):
            number = m.group(1).rstrip(".,;)–-")
            if (kind, number.lower()) in seen:
                continue
            seen.add((kind, number.lower()))
            out.append(Ref(kind=kind, title=title, number=number))
    return out


def _find(db: Session, org_id: uuid.UUID, ref: Ref, exclude: uuid.UUID) -> Document | None:
    """Документ архива с этим номером: в номере или названии («Сертификат 4512», «Протокол_56»), иначе — в тексте."""
    num = ref.number
    exact = re.compile(rf"(?<![\w]){re.escape(num)}(?![\w])", re.IGNORECASE)
    candidates = db.scalars(select(Document).where(
        Document.organization_id == org_id, Document.id != exclude,
        or_(Document.number.ilike(f"%{num}%"), Document.title.ilike(f"%{num}%"))).limit(50)).all()
    hits = [d for d in candidates if exact.search(f"{d.number or ''} {d.title.replace('_', ' ')}")]
    if hits:
        return sorted(hits, key=lambda d: (d.kind not in TARGET_KINDS[ref.kind], d.kind in ("aosr", "aook")))[0]
    # номер в тексте документа другого вида: «Сертификат соответствия № 4512», «Протокол № 56»
    row = db.execute(select(Document).join(KnowledgeSource, KnowledgeSource.document_id == Document.id)
                     .join(KnowledgeChunk, KnowledgeChunk.source_id == KnowledgeSource.id)
                     .where(Document.organization_id == org_id, Document.id != exclude,
                            Document.kind.notin_(["aosr", "aook"]),
                            or_(KnowledgeChunk.content.ilike(f"%№ {num}%"), KnowledgeChunk.content.ilike(f"%№{num}%")))
                     .limit(1)).first()
    return row[0] if row else None


def check_act(db: Session, doc: Document) -> ActCheck:
    text = _text(db, doc)
    works = ""
    m = re.search(r"(?:работ[ыа]?|предъявлены[^:]*)[:\s]+([^\n]{10,200})", text, re.IGNORECASE)
    if m:
        works = m.group(1).strip()
    act = ActCheck(document_id=str(doc.id), title=doc.title, date=doc.date.isoformat() if doc.date else None,
                   works=works or doc.title)
    if not text.strip():
        act.issues.append("Нет текста акта (скан без распознавания) — проверить вручную")
        return act
    act.refs = references(text)
    for ref in act.refs:
        found = _find(db, doc.organization_id, ref, doc.id)
        if found is not None:
            ref.found, ref.document_id, ref.document_title = True, str(found.id), found.title
    if not any(r.kind == "quality" for r in act.refs) and re.search(r"материал|арматур|бетон|смес|издели", text, re.I):
        act.issues.append("Материалы указаны, а документов о качестве (№) в акте нет")
    if not any(r.kind == "scheme" for r in act.refs) and WITH_SCHEME.search(text):
        act.issues.append("Нет ссылки на исполнительную схему")
    if not re.search(r"шифр|\d{2,}[-–]\d{2,}[-–]?[А-ЯA-Z]{1,4}\d?", text):
        act.issues.append("Не указан шифр рабочей документации")
    if not re.search(r"окончани\w*", text, re.I):
        act.issues.append("Не указана дата окончания работ")
    return act


def check_archive(db: Session, org_id: uuid.UUID, object_id: uuid.UUID | None = None, limit: int = 500) -> dict[str, Any]:
    stmt = select(Document).where(Document.organization_id == org_id, Document.kind.in_(["aosr", "aook"]))
    if object_id:
        stmt = stmt.where(or_(Document.object_id == object_id, Document.object_id.is_(None)))
    acts = [check_act(db, d) for d in db.scalars(stmt.order_by(Document.date.nulls_last(), Document.title).limit(limit))]
    missing = [(a, r) for a in acts for r in a.refs if not r.found]
    return {
        "acts": len(acts),
        "complete": sum(1 for a in acts if a.complete),
        "missing_refs": len(missing),
        "with_issues": sum(1 for a in acts if a.issues),
        "items": [asdict(a) | {"complete": a.complete} for a in acts],
    }


def summary_text(res: dict[str, Any], limit: int = 15) -> str:
    lines = [f"Актов: {res['acts']}, полных: {res['complete']}, не найдено в архиве ссылок: {res['missing_refs']}, "
             f"актов с замечаниями: {res['with_issues']}."]
    for a in res["items"]:
        if a["complete"]:
            continue
        miss = [f"{r['title']} № {r['number']}" for r in a["refs"] if not r["found"]]
        lines.append(f"• {a['title']}: " + "; ".join([*(f"нет в архиве: {m}" for m in miss), *a["issues"]]))
        if len(lines) > limit:
            lines.append("…")
            break
    return "\n".join(lines)
