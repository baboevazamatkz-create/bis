"""Понимание входящих: что это за сигнал и какие данные в нём.

Каждая функция сначала использует модель (если подключена), а без неё — правила. Схемы извлечения:
УПД/ТТН, сертификат/паспорт качества, протокол испытаний, отчёт прораба, фото площадки.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal

from pydantic import BaseModel, Field

from bis.llm.extract import extract, find_dates, parse_json
from bis.llm.gateway import LLMGateway
from bis.llm.types import LLMError, Message
from bis.voice import normalize_dictation

TYPES: dict[str, str] = {
    "upd": "Накладная на поставку материалов (УПД, ТОРГ-12, ТТН)",
    "certificate": "Документ о качестве (сертификат, паспорт, декларация)",
    "test_protocol": "Протокол лабораторных испытаний",
    "foreman_report": "Отчёт с площадки о выполненных работах, расходе, проблемах",
    "survey": "Геодезическая съёмка (точки проект/факт)",
    "letter": "Письмо",
    "contract": "Договор",
    "project_doc": "Проектная или рабочая документация",
    "site_photo": "Фото с площадки",
    "unknown": "Не удалось определить",
}

_RULES: list[tuple[str, str, float]] = [
    ("upd", r"универсальн\w+ передаточн|\bупд\b|товарн\w+ накладн|торг-12|транспортн\w+ накладн|\bттн\b|счет-фактур|счёт-фактур", 0.9),
    ("certificate", r"сертификат\w* (?:соответстви|качеств)|документ\w* о качеств|паспорт\w* (?:качеств|издели|продукци)|декларац\w+ о соответстви", 0.9),
    ("test_protocol", r"протокол\w* (?:лабораторн\w+ )?испытани|протокол\w* определени\w+ прочност", 0.9),
    ("contract", r"договор\w*[^\n]{0,80}\n?[\s\S]{0,400}(?:именуем|предмет договор)", 0.8),
    ("letter", r"\bисх\.?\s*№|уважаем\w+ [А-ЯЁ]", 0.7),
]
_WORK_VERBS = r"залил|вязал|связал|забетонир|уложил|смонтир|выполнил|выполнен|сделал|армиров|засыпал|установил|выкопал|разработал|" \
              r"заармир|смонтирова|провели|закончил|начал"
_PROBLEM = r"не привез|не завез|простой|простаива|стоял[аи]? (?:\w+ )?(?:час|смен|день|полдня)|сломал|поломк|не хватает|нехватк|задерж|авари|травм|проблем|нет (?:бетон|арматур|материал|людей|техник)"


def classify(gateway: LLMGateway | None, text: str, filename: str | None, kind: str) -> tuple[str, float, str]:
    """(тип, уверенность, кто определил: правила или модель)."""
    low = (text or "").lower()
    name = (filename or "").lower()
    if kind == "photo" and len(low) < 40:
        return "site_photo", 0.6, "rules"
    if name.endswith((".csv", ".txt", ".xlsx")) and re.search(r"x\s*пр|xпр|проект\w*\s*x", low[:500]):
        return "survey", 0.95, "rules"
    for t, pattern, conf in _RULES:
        if re.search(pattern, low[:4000]):
            return t, conf, "rules"
    from bis.services.project_docs import detect_section

    if kind == "document" and detect_section(filename or "", low[:2000]).get("stage"):
        return "project_doc", 0.75, "rules"
    if kind in ("text", "voice") and re.search(_WORK_VERBS + "|" + _PROBLEM + r"|израсходова|расход|ушл[оаи]\b", low):
        return "foreman_report", 0.8, "rules"
    if gateway is not None and gateway.has_capability("text", "route"):
        catalog = "\n".join(f"- {k}: {v}" for k, v in TYPES.items())
        try:
            resp = gateway.complete([
                Message(role="system", content="Определи тип входящего с объекта строительства. Ответь JSON "
                        '{"type": "<код>", "confidence": 0..1}. Типы:\n' + catalog),
                Message(role="user", content=f"Файл: {filename or '—'}\n{(text or '')[:3000]}"),
            ], task="route", json_mode=True, max_tokens=100)
            data = parse_json(resp.message.content) or {}
            if data.get("type") in TYPES:
                return data["type"], float(data.get("confidence") or 0.7), resp.provider
        except LLMError:
            pass
    if kind in ("text", "voice"):
        return "foreman_report", 0.5, "rules"
    return "unknown", 0.3, "rules"


# --- общие разборщики --------------------------------------------------------------------------

UNIT_RE = r"шт|м3|м2|м³|м²|п\.\s?м|пог\.?\s?м|т|кг|компл|м|л"
_NUM = r"(?<![\w.,])(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d+)?"


def num(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(re.sub(r"[\s ]", "", v).replace(",", "."))
    except ValueError:
        return None


def norm_unit(u: str) -> str:
    u = re.sub(r"\s+", "", u.lower()).replace("м³", "м3").replace("м²", "м2").replace("пог.м", "п.м").replace("пм", "п.м")
    return {"тн": "т", "тонн": "т", "куб": "м3"}.get(u, u)


def _doc_number(text: str, words: str) -> tuple[str | None, dt.date | None]:
    m = re.search(rf"(?:{words})[^\n№]{{0,60}}№\s*([\w\-/.]+)\s*(?:от\s*([^\n]{{6,30}}))?", text, re.IGNORECASE)
    if not m:
        return None, None
    dates = find_dates(m.group(2) or "")
    return m.group(1).rstrip(".,"), dates[0][1] if dates else None


# --- УПД / накладная -----------------------------------------------------------------------------


class SupplyItem(BaseModel):
    name: str
    unit: str
    quantity: float
    price: float | None = Field(None, description="Цена за единицу без НДС")


class SupplyDoc(BaseModel):
    number: str | None = None
    date: dt.date | None = None
    supplier: str | None = None
    supplier_inn: str | None = None
    items: list[SupplyItem] = Field(default_factory=list)
    certificates_mentioned: list[str] = Field(default_factory=list, description="Упомянутые документы о качестве")


def _table_items(text: str) -> list[SupplyItem]:
    items = []
    for line in text.splitlines():
        cells = [c.strip() for c in re.split(r"\s*\|\s*|\t", line) if c.strip()]
        if len(cells) < 3:
            continue
        unit_idx = next((i for i, c in enumerate(cells) if re.fullmatch(rf"(?:{UNIT_RE})\.?", c.lower())), None)
        if unit_idx is None or unit_idx == 0:
            continue
        name = next((c for c in reversed(cells[:unit_idx]) if re.search(r"[А-Яа-яA-Za-z]{3}", c)), None)
        nums = [num(c) for c in cells[unit_idx + 1:] if re.fullmatch(_NUM, c)]
        if not name or not nums or nums[0] is None or nums[0] <= 0:
            continue
        items.append(SupplyItem(name=name, unit=norm_unit(cells[unit_idx]), quantity=nums[0],
                                price=nums[1] if len(nums) > 1 else None))
    return items


def heuristic_supply(text: str) -> SupplyDoc:
    number, date = _doc_number(text, r"упд|универсальн\w+ передаточн\w+ документ|счет-фактур\w*|счёт-фактур\w*|накладн\w+")
    supplier = None
    m = re.search(r"(?:продавец|поставщик|грузоотправитель)\s*:?\s*((?:ООО|АО|ПАО|ЗАО|ИП)\s*[«\"][^»\"\n]+[»\"])", text, re.IGNORECASE)
    if m:
        supplier = m.group(1).strip()
    inn = re.search(r"ИНН(?:/КПП)?\s*(?:продавца)?\s*:?\s*(\d{10}|\d{12})", text)
    certs = re.findall(r"(?:сертификат|паспорт|документ о качестве)[^\n]{0,80}", text, re.IGNORECASE)
    return SupplyDoc(number=number, date=date, supplier=supplier, supplier_inn=inn.group(1) if inn else None,
                     items=_table_items(text), certificates_mentioned=certs[:5])


def read_supply(gateway: LLMGateway | None, text: str, images: list[dict] | None = None, hints: str = ""):
    return extract(gateway, SupplyDoc, text, images=images, task="extract",
                   instructions=("Ты читаешь накладную на поставку строительных материалов (УПД, ТОРГ-12, ТТН). "
                                "Извлеки номер, дату, поставщика, ИНН и позиции: наименование, единица, количество, "
                                "цена без НДС. Строки «Итого» не включай.") + hints,
                   fallback=heuristic_supply)


# --- документ о качестве --------------------------------------------------------------------------


class QualityDoc(BaseModel):
    doc_type: Literal["certificate", "passport", "declaration", "other"] = "certificate"
    number: str | None = None
    date: dt.date | None = None
    material: str | None = Field(None, description="Продукция, марка, класс")
    batch: str | None = None
    manufacturer: str | None = None
    valid_until: dt.date | None = None


_MATERIAL_WORDS = r"бетон|арматур|песок|щеб|мембран|кирпич|цемент|раствор|труб|кабел|металлопрокат|утеплит|блок|плит"


def heuristic_quality(text: str) -> QualityDoc:
    low = text.lower()
    kind = "passport" if "паспорт" in low[:500] else "declaration" if "декларац" in low[:500] else "certificate"
    number, date = _doc_number(text, r"сертификат\w*|паспорт\w*|документ\w* о качестве|декларац\w*")
    material = None
    m = re.search(r"(?:продукция|наименование продукции|наименование|материал|изделие|марка|бетонная смесь|"
                  r"растворная смесь|смесь)\s*:\s*([^\n]{3,120})", text, re.IGNORECASE)
    if m:
        material = m.group(1).strip(" .;")
    else:
        lines = [ln.strip() for ln in text.splitlines()
                 if re.search(_MATERIAL_WORDS, ln.lower()) and not re.search(r"сертификат|паспорт|документ\w* о качеств|"
                                                                                r"декларац|протокол", ln.lower())]
        graded = [ln for ln in lines if re.search(r"\b[BВМMАA]\d{2,3}\b|\b[WF]\d+", ln)]
        if graded or lines:
            material = (graded or lines)[0].split(":", 1)[-1].strip(" .;")[:120]
    batch = re.search(r"парти\w*\s*№?\s*([\w\-/]+)", text, re.IGNORECASE)
    valid = re.search(r"(?:действителен до|срок действия[^\n]{0,15}до)\s*([^\n]{6,25})", text, re.IGNORECASE)
    manuf = re.search(r"(?:изготовитель|производитель)\s*:?\s*([^\n]{3,120})", text, re.IGNORECASE)
    vd = find_dates(valid.group(1)) if valid else []
    return QualityDoc(doc_type=kind, number=number, date=date, material=material, batch=batch.group(1) if batch else None,
                      manufacturer=manuf.group(1).strip() if manuf else None, valid_until=vd[0][1] if vd else None)


def read_quality(gateway: LLMGateway | None, text: str, images: list[dict] | None = None, hints: str = ""):
    return extract(gateway, QualityDoc, text, images=images, task="extract",
                   instructions=("Ты читаешь документ о качестве строительного материала (сертификат, паспорт, декларацию). "
                                "Извлеки вид, номер, дату, продукцию (марку, класс), партию, изготовителя, срок действия.") + hints,
                   fallback=heuristic_quality)


# --- протокол испытаний ---------------------------------------------------------------------------


class TestProtocol(BaseModel):
    number: str | None = None
    date: dt.date | None = None
    structure: str | None = Field(None, description="Конструкция / объект испытаний")
    test: str | None = Field(None, description="Вид испытания")
    required: str | None = None
    actual: str | None = None
    compliant: bool | None = None


def heuristic_protocol(text: str) -> TestProtocol:
    low = text.lower()
    number, date = _doc_number(text, r"протокол\w*")
    structure = re.search(r"(?:конструкция|объект испытаний|наименование конструкции|место отбора)\s*:?\s*([^\n]{3,150})",
                          text, re.IGNORECASE)
    test = re.search(r"(?:вид испытани\w*|определение)\s*:?\s*([^\n]{3,120})", text, re.IGNORECASE)
    required = re.search(r"(?:требуем\w*|проектн\w*)[^\n]{0,30}?(?:класс\w*\s*)?(B\s?\d+(?:[.,]\d)?|\d+(?:[.,]\d+)?\s*МПа)", text, re.IGNORECASE)
    actual = re.search(r"(?:фактическ\w*|средн\w*)[^\n]{0,30}?(\d+(?:[.,]\d+)?\s*МПа)", text, re.IGNORECASE)
    compliant = False if "не соответствует" in low else True if "соответствует" in low else None
    return TestProtocol(number=number, date=date, structure=structure.group(1).strip() if structure else None,
                        test=test.group(1).strip() if test else None, required=required.group(1) if required else None,
                        actual=actual.group(1) if actual else None, compliant=compliant)


def read_protocol(gateway: LLMGateway | None, text: str, images: list[dict] | None = None, hints: str = ""):
    return extract(gateway, TestProtocol, text, images=images, task="extract",
                   instructions=("Ты читаешь протокол лабораторных испытаний на стройке. Извлеки номер, дату, конструкцию, "
                                "вид испытания, требуемое и фактическое значение, соответствие требованиям.") + hints,
                   fallback=heuristic_protocol)


# --- отчёт прораба ----------------------------------------------------------------------------------


class ReportedWork(BaseModel):
    description: str
    quantity: float | None = None
    unit: str | None = None
    location: str | None = Field(None, description="Оси, захватка, этаж, отметка")


class ReportedMaterial(BaseModel):
    name: str
    quantity: float
    unit: str


class FieldReport(BaseModel):
    works: list[ReportedWork] = Field(default_factory=list)
    materials: list[ReportedMaterial] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


_LOC = re.compile(r"(в\s+ос(?:ях|и)\s+[\wА-Яа-я\-/,.]+(?:\s*/\s*[\wА-Яа-я\-]+)?|захватк\w*\s*№?\s*\d+(?:\s*[-и,]\s*\d+(?![\d.,]*\s*(?:" + UNIT_RE + r")\b))*|"
                  r"\d+\s*этаж\w*|этаж\w*\s*\d+|на отм(?:етке)?\.?\s*[+-]?\d+[.,]?\d*)", re.IGNORECASE)
_QTY = re.compile(rf"({_NUM})\s*({UNIT_RE})\b\.?", re.IGNORECASE)


_SPOKEN_UNITS = [(r"\bкуб(?:ов|а|ы|ик\w*)?\b|\bкубометр\w*", "м3"), (r"\bквадрат(?:ов|а|ы)?\b", "м2"),
                 (r"\bтонн?\w*", "т"), (r"\bштук\w*", "шт")]


def heuristic_report(text: str) -> FieldReport:
    text = normalize_dictation(text)
    for pattern, unit in _SPOKEN_UNITS:
        text = re.sub(pattern, unit, text, flags=re.IGNORECASE)
    report = FieldReport()
    sentences = re.split(r"(?<=[.!?;\n])\s+|\n", text)
    # «залили 290 м3, бетона ушло 296 м3» — расход отдельной частью
    clauses = [c for s_ in sentences for c in re.split(r",\s*(?=[^,]*(?:израсход|ушл[оаи]\b|расход|использова|потрати))",
                                                       s_, flags=re.IGNORECASE)]
    for sent in clauses:
        sent = sent.strip()
        if not sent:
            continue
        low = sent.lower()
        q = _QTY.search(sent)
        loc = _LOC.search(sent)
        if re.search(_PROBLEM, low):
            report.problems.append(sent)
            continue
        verb = re.search(r"израсходова\w*|ушл[оаи]\b|расход\w*|использова\w*|потрати\w*", sent, re.IGNORECASE)
        if verb and q:
            after = _QTY.sub("", sent[verb.end():]).strip(" ,.:—-")
            after = re.sub(r"^(?:на|по)\s+.*$", "", after, flags=re.IGNORECASE).strip()
            before = _QTY.sub("", sent[:verb.start()]).strip(" ,.:—-")
            name = after or re.sub(r"^(?:сегодня|вчера|всего|итого)\s+", "", before, flags=re.IGNORECASE)
            report.materials.append(ReportedMaterial(name=name or sent, quantity=num(q.group(1)) or 0, unit=norm_unit(q.group(2))))
            continue
        if re.search(_WORK_VERBS, low) or q:
            report.works.append(ReportedWork(description=sent, quantity=num(q.group(1)) if q else None,
                                             unit=norm_unit(q.group(2)) if q else None,
                                             location=loc.group(1) if loc else None))
    return report


def _clean_report(r: FieldReport | None) -> FieldReport | None:
    """Проверка ответа модели: только настоящие единицы измерения; расход материала — не работа."""
    if r is None:
        return r
    unit_ok = lambda u: bool(u) and re.fullmatch(rf"(?:{UNIT_RE})\.?", norm_unit(u)) is not None  # noqa: E731
    mats = {(m.quantity, norm_unit(m.unit)) for m in r.materials}
    works = []
    for w in r.works:
        if w.unit and not unit_ok(w.unit):
            continue  # «по накладным», «машин» — не объём работы
        if re.search(r"расход|ушло|израсход", w.description.lower()) or (w.quantity, norm_unit(w.unit or "")) in mats \
                and not re.search(_WORK_VERBS, w.description.lower()):
            continue
        works.append(w.model_copy(update={"unit": norm_unit(w.unit) if w.unit else None}))
    materials = []
    for m in r.materials:
        unit = norm_unit(m.unit) if unit_ok(m.unit) else None
        if unit is None:  # единицу берём у работы, к которой относится материал (бетон → м3)
            unit = next((w.unit for w in works if w.unit and m.name[:4].lower() in w.description.lower()), None)
        if unit:
            materials.append(m.model_copy(update={"unit": unit}))
    return r.model_copy(update={"works": works, "materials": materials})


def read_report(gateway: LLMGateway | None, text: str, hints: str = ""):
    res = extract(gateway, FieldReport, normalize_dictation(text), task="extract",
                  instructions=("Ты разбираешь сообщение прораба с площадки (часто разговорное). "
                                "works — только выполненные сегодня работы: описание, объём, единица (м3, м2, т, шт, п.м), "
                                "место (оси, захватка, этаж). Планы на завтра — не работы. "
                                "materials — израсходованные материалы с количеством и единицей (м3, т, м2, шт); "
                                "расход материала НЕ дублируй в works. problems — простои, нехватка, поломки, нарушения.\n"
                                "Пример: «залили 290 кубов на захватке 4, бетона ушло 296» → works: [{description: "
                                "\"Бетонирование, захватка 4\", quantity: 290, unit: \"м3\", location: \"захватка 4\"}], "
                                "materials: [{name: \"бетон\", quantity: 296, unit: \"м3\"}].") + hints,
                  fallback=heuristic_report)
    if not res.used_fallback:
        res.data = _clean_report(res.data)
    return res


# --- фото площадки (только модель зрения) -------------------------------------------------------


class Defect(BaseModel):
    description: str
    severity: Literal["low", "medium", "high"] = "medium"


class SitePhoto(BaseModel):
    is_document: bool = Field(False, description="На фото документ (накладная, сертификат), а не площадка")
    document_type: str | None = None
    work_type: str | None = Field(None, description="Вид работ на фото")
    description: str = ""
    completion_percent: int | None = Field(None, description="Оценка готовности видимой работы, %")
    location_hint: str | None = None
    defects: list[Defect] = Field(default_factory=list)
    safety_violations: list[str] = Field(default_factory=list, description="Нарушения охраны труда: без касок, без ограждения…")


def see_photo(gateway: LLMGateway | None, image: dict, caption: str = "", hints: str = ""):
    return extract(gateway, SitePhoto, caption, images=[image], task="vision",
                   instructions=("Ты инженер строительного контроля и смотришь фото с объекта. Если это фото документа — "
                                "is_document=true и вид документа. Иначе: вид работ, что видно, оценка готовности, место "
                                "(если видно маркировку осей), дефекты (оголение арматуры, раковины, трещины, отклонения) и "
                                "нарушения охраны труда (люди без касок/жилетов, нет ограждений, работа на высоте без страховки). "
                                "Не придумывай: если не видно — оставь пустым.") + hints)
