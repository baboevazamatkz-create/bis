"""Анализ проектной и рабочей документации.

1. Комплектность: раздел определяется по шифру и заголовку (ПЗ, ПЗУ, АР, КР, ИОС1–ИОС7, ПОС, ООС, ПБ …),
   сравнивается с перечнем разделов ПД по Положению о составе разделов ПД (ПП РФ № 87).
   Перечень хранится как данные: сверяйте с действующей редакцией и дополняйте под объект.
2. Спецификации/ведомости: позиции «наименование — ед. — количество» извлекаются из текста (модель или правила).
3. Сверка спецификации со сметой договора: расхождения объёмов и позиции, которых нет в смете.
4. Ведомость замечаний (DOCX).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from docx.enum.text import WD_ALIGN_PARAGRAPH
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.documents.common import borderless_table, new_document, para, to_bytes
from bis.llm.extract import extract
from bis.llm.gateway import LLMGateway
from bis.models import Document, EstimateItem
from bis.services import works as wsvc


@dataclass(frozen=True)
class Section:
    code: str
    title: str
    pattern: str
    required: bool = True


PD_SECTIONS: list[Section] = [
    Section("ПЗ", "Пояснительная записка", r"пояснительн\w* записк"),
    Section("ПЗУ", "Схема планировочной организации земельного участка", r"планировочн\w* организаци\w* земельн"),
    Section("АР", "Объёмно-планировочные и архитектурные решения", r"архитектурн\w* решени"),
    Section("КР", "Конструктивные решения", r"конструктивн\w* (?:и объемно-планировочн\w* )?решени"),
    Section("ИОС1", "Система электроснабжения", r"электроснабжени"),
    Section("ИОС2", "Система водоснабжения", r"водоснабжени"),
    Section("ИОС3", "Система водоотведения", r"водоотведени|канализаци"),
    Section("ИОС4", "Отопление, вентиляция и кондиционирование воздуха, тепловые сети", r"отоплени\w*,? вентиляци"),
    Section("ИОС5", "Сети связи", r"сети связи|сетей связи"),
    Section("ИОС6", "Система газоснабжения", r"газоснабжени", required=False),
    Section("ТХ", "Технологические решения", r"технологическ\w* решени", required=False),
    Section("ПОС", "Проект организации строительства", r"проект\w* организаци\w* строительств"),
    Section("ПОД", "Проект организации работ по сносу (демонтажу)", r"\bснос\w*|демонтаж\w* объект", required=False),
    Section("ООС", "Мероприятия по охране окружающей среды", r"охран\w* окружающ"),
    Section("ПБ", "Мероприятия по обеспечению пожарной безопасности", r"пожарн\w* безопасност"),
    Section("ТБЭ", "Требования к обеспечению безопасной эксплуатации", r"безопасн\w* эксплуатаци"),
    Section("ОДИ", "Мероприятия по обеспечению доступа инвалидов", r"доступ\w* инвалид|маломобильн"),
    Section("ЭЭ", "Мероприятия по обеспечению энергетической эффективности", r"энергетическ\w* эффективност"),
    Section("СМ", "Смета на строительство", r"сметн\w* (?:документаци|расч)|смета на строительств"),
]
# Марки основных комплектов рабочей документации (ГОСТ Р 21.101)
RD_MARKS = ["АР", "АС", "КЖ", "КЖИ", "КМ", "КМД", "КД", "ГП", "ГТ", "ОВ", "ВК", "НВК", "ЭОМ", "ЭМ", "ЭО", "ЭН", "СС", "ПС",
            "АОВ", "АК", "ТХ", "ТС", "ИОС", "ПОС", "ППР"]
_CODE_RE = re.compile(r"(?:^|[\s\-_./])(ПЗУ|ПЗ|АР|КР|ИОС\s?\d|ТХ|ПОС|ПОД|ООС|ПБ|ТБЭ|ОДИ|ЭЭ|СМ)(?:\d{0,2})(?=$|[\s\-_./.(])",
                      re.IGNORECASE)
_RD_RE = re.compile(r"(?:^|[\s\-_./])(" + "|".join(sorted(RD_MARKS, key=len, reverse=True)) + r")(\d{0,2})(?=$|[\s\-_./(])")


def detect_section(title: str, text_head: str = "") -> dict[str, str | None]:
    """Раздел ПД или марка РД по шифру в названии/имени файла, затем по заголовку в тексте."""
    m = _CODE_RE.search(title.upper())
    if m:
        code = m.group(1).upper().replace(" ", "")
        return {"stage": "ПД", "code": code, "title": _title(code)}
    m = _RD_RE.search(title.upper())
    if m:
        return {"stage": "РД", "code": m.group(1) + (m.group(2) or ""), "title": None}
    head = (title + "\n" + text_head[:3000]).lower()
    for s in PD_SECTIONS:
        if re.search(s.pattern, head):
            return {"stage": "ПД", "code": s.code, "title": s.title}
    return {"stage": None, "code": None, "title": None}


def _title(code: str) -> str | None:
    return next((s.title for s in PD_SECTIONS if s.code == code), None)


def completeness(db: Session, org_id: uuid.UUID, object_id: uuid.UUID) -> dict:
    docs = db.scalars(select(Document).where(Document.organization_id == org_id, Document.object_id == object_id,
                                             Document.kind == "project_doc")).all()
    present: dict[str, list[str]] = {}
    rd: dict[str, list[str]] = {}
    unknown: list[str] = []
    for d in docs:
        sec = (d.data or {}).get("pd_section") or detect_section(d.title or d.file_name or "")
        if sec.get("stage") == "ПД":
            present.setdefault(sec["code"], []).append(d.title)
        elif sec.get("stage") == "РД":
            rd.setdefault(sec["code"], []).append(d.title)
        else:
            unknown.append(d.title)
    sections = [{"code": s.code, "title": s.title, "required": s.required, "present": s.code in present,
                 "documents": present.get(s.code, [])} for s in PD_SECTIONS]
    missing = [f"{s.code} — {s.title}" for s in PD_SECTIONS if s.required and s.code not in present]
    return {"sections": sections, "missing_required": missing, "rd_sets": rd, "unrecognized": unknown,
            "documents": len(docs),
            "note": "Перечень разделов — по ПП РФ № 87; проверьте применимость к объекту и действующую редакцию."}


# --- спецификации --------------------------------------------------------------------------------

UNITS = r"шт|м3|м2|м³|м²|п\.\s?м|пог\.?\s?м|м|т|кг|компл|комплект|л|упак"
_SEP = r"(?:\s*\|\s*|\s+)"  # единица и количество отделены пробелом или границей ячейки


class SpecItem(BaseModel):
    position: str | None = None
    name: str
    unit: str
    quantity: float
    mark: str | None = Field(None, description="Марка, тип, обозначение документа")


class Specification(BaseModel):
    items: list[SpecItem] = Field(default_factory=list)


_SPEC_LINE = re.compile(
    rf"^\s*(?P<pos>\d{{1,3}}(?:\.\d{{1,2}})?)?[\s|.)]*(?P<name>[А-ЯЁA-Z][^|]{{3,200}}?){_SEP}"
    rf"(?P<unit>{UNITS})\.?{_SEP}(?P<qty>\d+(?:[.,]\d+)?)\s*(?:\|.*)?$", re.IGNORECASE)


def _num(v: str) -> float | None:
    try:
        return float(Decimal(v.replace(",", ".").replace(" ", "")))
    except InvalidOperation:
        return None


def heuristic_spec(text: str) -> Specification:
    items = []
    for line in text.splitlines():
        m = _SPEC_LINE.match(line.strip())
        if not m:
            continue
        qty = _num(m.group("qty"))
        name = m.group("name").strip(" |-")
        if qty is None or qty <= 0 or len(name) < 4:
            continue
        unit = re.sub(r"\s+", "", m.group("unit").lower()).replace("м³", "м3").replace("м²", "м2").replace("пог.м", "п.м")
        items.append(SpecItem(position=m.group("pos"), name=name, unit=unit, quantity=qty))
    return Specification(items=items)


SPEC_INSTRUCTIONS = """Извлеки из проектного документа позиции спецификаций и ведомостей объёмов работ/материалов:
позиция, наименование (с маркой/типоразмером), единица измерения, количество. Строки «Итого» не включай."""


def extract_specification(gateway: LLMGateway | None, text: str) -> tuple[Specification, bool]:
    res = extract(gateway, Specification, text, instructions=SPEC_INSTRUCTIONS, task="extract", window_chars=25000,
                  fallback=heuristic_spec)
    return res.data or Specification(), res.used_fallback


def _unit(u: str) -> str:
    return re.sub(r"[\s.]", "", u.lower()).replace("пм", "м").replace("погм", "м")


def compare_with_estimate(db: Session, org_id: uuid.UUID, contract_id: uuid.UUID, spec: Specification,
                          tolerance_pct: float = 5.0) -> dict:
    items = db.scalars(select(EstimateItem).where(EstimateItem.organization_id == org_id,
                                                  EstimateItem.contract_id == contract_id)).all()
    rows, missing = [], []
    for s in spec.items:
        best, score = None, 0.0
        for it in items:
            if _unit(it.unit) != _unit(s.unit):
                continue
            sc = wsvc.similarity(s.name, it.name)
            if sc > score:
                best, score = it, sc
        if best is None or score < 0.3:
            missing.append({"name": s.name, "unit": s.unit, "quantity": s.quantity})
            continue
        est = float(best.quantity)
        diff = s.quantity - est
        pct = round(diff / est * 100, 1) if est else None
        rows.append({"spec_name": s.name, "estimate_item": best.name, "code": best.code, "unit": best.unit,
                     "spec_quantity": s.quantity, "estimate_quantity": est, "difference": round(diff, 4),
                     "difference_pct": pct, "match": round(score, 2),
                     "status": "ok" if pct is not None and abs(pct) <= tolerance_pct else "mismatch"})
    return {"rows": rows, "not_in_estimate": missing,
            "mismatches": [r for r in rows if r["status"] == "mismatch"], "tolerance_pct": tolerance_pct}


# --- ведомость замечаний --------------------------------------------------------------------------


class Remark(BaseModel):
    section: str = Field(description="Раздел/марка, лист")
    text: str = Field(description="Суть замечания")
    norm: str | None = Field(None, description="Ссылка на норму: документ и пункт")
    severity: str = Field("значительное", description="критическое | значительное | незначительное")


def render_remarks(*, object_name: str, project: str, remarks: list[Remark], author: str) -> bytes:
    doc = new_document(font_size=11, margins_cm=(2.0, 1.0, 1.5, 1.5))
    para(doc, "ВЕДОМОСТЬ ЗАМЕЧАНИЙ", bold=True, size=13, align=WD_ALIGN_PARAGRAPH.CENTER)
    para(doc, f"к проектной (рабочей) документации: {project}", align=WD_ALIGN_PARAGRAPH.CENTER)
    para(doc, f"Объект: {object_name}", align=WD_ALIGN_PARAGRAPH.CENTER, space_after=8)
    table = doc.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    for i, h in enumerate(["№", "Раздел, лист", "Замечание", "Нормативное обоснование", "Значимость", "Ответ проектировщика"]):
        table.rows[0].cells[i].text = h
    for n, r in enumerate(remarks, 1):
        cells = table.add_row().cells
        for i, v in enumerate([str(n), r.section, r.text, r.norm or "—", r.severity, ""]):
            cells[i].text = v
    para(doc)
    sig = borderless_table(doc, 1, 2, [11.0, 6.0])
    sig.cell(0, 0).text = f"Составил: {author}"
    sig.cell(0, 1).text = "_______________"
    return to_bytes(doc)
