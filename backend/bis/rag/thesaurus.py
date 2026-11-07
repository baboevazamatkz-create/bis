"""Строительный тезаурус: аббревиатуры и синонимы для расширения поисковых запросов.

Расширение улучшает полнотекстовый поиск («ИД» находит «исполнительная документация»,
«засыпка пазух» — «обратная засыпка»). Организация может добавить свои термины в память (kind="term",
формат «ТЕРМИН = синоним1; синоним2»).
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

BASE: dict[str, list[str]] = {
    "ид": ["исполнительная документация"],
    "аоср": ["акт освидетельствования скрытых работ"],
    "аоок": ["акт освидетельствования ответственных конструкций"],
    "кс-2": ["акт о приемке выполненных работ"],
    "кс-3": ["справка о стоимости выполненных работ"],
    "м-29": ["отчет о расходе основных материалов"],
    "гпр": ["график производства работ"],
    "ппр": ["проект производства работ"],
    "пос": ["проект организации строительства"],
    "смр": ["строительно-монтажные работы"],
    "пд": ["проектная документация"],
    "рд": ["рабочая документация"],
    "жбк": ["железобетонные конструкции"],
    "нрс": ["национальный реестр специалистов"],
    "ск": ["строительный контроль"],
    "обратная засыпка": ["засыпка пазух", "засыпка котлована"],
    "засыпка пазух": ["обратная засыпка"],
    "арматура": ["армирование", "арматурные работы"],
    "армирование": ["арматура", "арматурные работы"],
    "бетонирование": ["бетонная смесь", "укладка бетона"],
    "опалубка": ["опалубочные работы", "распалубка"],
    "распалубка": ["демонтаж опалубки"],
    "гидроизоляция": ["изоляция подземных конструкций"],
    "допуск": ["предельное отклонение", "допустимое отклонение"],
    "отклонение": ["допуск", "предельное отклонение"],
    "уход за бетоном": ["выдерживание бетона"],
    "прочность бетона": ["класс бетона", "испытание образцов"],
    "защитный слой": ["толщина защитного слоя бетона"],
    "сертификат": ["документ о качестве", "паспорт качества"],
    "неустойка": ["пени", "штраф"],
    "пени": ["неустойка"],
}


def org_terms(db: Session | None, org_id: uuid.UUID | None) -> dict[str, list[str]]:
    if db is None or org_id is None:
        return {}
    from bis.models import OrgMemory

    terms: dict[str, list[str]] = {}
    rows = db.scalars(select(OrgMemory.content).where(OrgMemory.organization_id == org_id, OrgMemory.kind == "term",
                                                      OrgMemory.is_active.is_(True)))
    for content in rows:
        if "=" in content:
            key, vals = content.split("=", 1)
            terms[key.strip().lower()] = [v.strip() for v in re.split(r"[;,]", vals) if v.strip()]
    return terms


_WORD = re.compile(r"[а-яa-z0-9]+(?:-[а-яa-z0-9]+)?")


def _stem(word: str) -> str:
    """Грубая основа слова: «обратной» и «обратная» → «обратн» (аббревиатуры не усекаются)."""
    return word if len(word) <= 4 else word[: max(4, len(word) - 2)]


def _matches(term: str, words: list[str]) -> bool:
    stems = [_stem(w) for w in _WORD.findall(term)]
    if len(stems) == 1 and len(stems[0]) <= 4:
        return stems[0] in words  # аббревиатура — точное совпадение слова
    return all(any(w.startswith(st) for w in words) for st in stems)


def expand(query: str, extra: dict[str, list[str]] | None = None) -> list[str]:
    """Синонимы и расшифровки терминов, встречающихся в запросе (с учётом склонений), без самого запроса."""
    low = query.lower().replace("ё", "е")
    words = _WORD.findall(low)
    thesaurus = {**BASE, **(extra or {})}
    out: list[str] = []
    for term, synonyms in thesaurus.items():
        if _matches(term.lower().replace("ё", "е"), words):
            out.extend(s for s in synonyms if s.lower() not in low and s not in out)
    return out
