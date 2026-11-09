"""Карточка документа: что это, к чему относится, что внутри, на что ссылается.

Номера, даты, шифры РД, листы, изменения, оси, отметки, марки материалов, нормы и ссылки на другие документы —
правилами (точно, без ошибок модели). Суть, конструкции, работы, стороны — моделью; без модели — правилами.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal

from pydantic import BaseModel, Field

CARD_VERSION = 3  # rebuild cards that previously read only the first 8000 characters

KIND_TITLES = {
    "aosr": "АОСР", "aook": "АООК", "scheme": "Исполнительная схема", "ks2": "КС-2", "ks3": "КС-3", "order": "Приказ",
    "work_journal": "Журнал работ", "contract": "Договор", "letter": "Письмо", "incoming_letter": "Письмо",
    "project_doc": "Проектная документация", "upd": "УПД", "supply_doc": "Документ поставки", "certificate": "Документ о качестве",
    "test_protocol": "Протокол испытаний", "survey": "Съёмка", "site_photo": "Фото", "photo": "Фото", "other": "Документ",
    "unknown": "Документ", "foreman_report": "Отчёт",
}


class MaterialRef(BaseModel):
    name: str = Field(description="Материал или изделие: «арматура», «бетонная смесь», «мастика»")
    grade: str | None = Field(None, description="Марка, класс, диаметр: «А500С d12», «B25 W6 F150»")
    quality_doc: str | None = Field(None, description="Документ о качестве из текста: «паспорт № 118-131»")


class DocRef(BaseModel):
    kind: Literal["quality", "protocol", "scheme", "letter", "contract", "rd", "act", "other"]
    number: str
    date: str | None = None
    label: str | None = None


class Card(BaseModel):
    version: int = CARD_VERSION
    kind: str = "unknown"
    title: str = ""  # «АОСР № 1 от 12.04.2026 — армирование фундаментной плиты Фм1»
    number: str | None = None
    date: str | None = None
    sender: str | None = None
    recipient: str | None = None
    parties: list[str] = Field(default_factory=list)
    rd_code: str | None = None  # шифр: «17-2026-КЖ0»
    rd_mark: str | None = None  # марка комплекта: «КЖ0», «АС»
    sheets: list[str] = Field(default_factory=list)
    revision: str | None = None
    elements: list[str] = Field(default_factory=list)  # конструкции: «фундаментная плита Фм1»
    works: list[str] = Field(default_factory=list)
    materials: list[MaterialRef] = Field(default_factory=list)
    grades: list[str] = Field(default_factory=list)  # «B25 W6 F150», «А500С», «d12»
    axes: list[str] = Field(default_factory=list)
    levels: list[str] = Field(default_factory=list)
    norms: list[str] = Field(default_factory=list)
    refs: list[DocRef] = Field(default_factory=list)
    work_start: str | None = None
    work_end: str | None = None
    deadline: str | None = None
    summary: str | None = None
    by: str = "rules"
    vision: dict | None = None  # что увидела модель зрения на листе (схема, чертёж)
    text_chars: int = 0
    model_read_complete: bool | None = None

    def label(self) -> str:
        """Шапка для поиска и ссылок."""
        head = KIND_TITLES.get(self.kind, "Документ")
        if self.number:
            head += f" № {self.number}"
        if self.date:
            head += f" от {dt.date.fromisoformat(self.date):%d.%m.%Y}"
        about = self.summary or ", ".join(self.elements[:2]) or ", ".join(self.works[:1])
        bits = [head]
        if self.rd_code:
            bits.append(f"РД {self.rd_code}" + (f" л. {', '.join(self.sheets[:4])}" if self.sheets else "")
                        + (f" изм. {self.revision}" if self.revision else ""))
        if about:
            bits.append(about[:160])
        return " — ".join(bits)

    def search_text(self) -> str:
        """Плоский текст карточки для поиска по сущностям (марки, оси, конструкции, номера)."""
        parts = [self.title, self.number or "", self.rd_code or "", " ".join(self.sheets), " ".join(self.elements),
                 " ".join(self.works), " ".join(f"{m.name} {m.grade or ''}" for m in self.materials), " ".join(self.grades),
                 " ".join(self.axes), " ".join(self.levels), " ".join(self.norms),
                 " ".join(f"{r.kind}:{r.number}" for r in self.refs), self.sender or "", self.recipient or "", self.summary or ""]
        return " ".join(p for p in parts if p)


# --- правила -------------------------------------------------------------------------------------

_LATIN = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")
MARKS = (r"(?:АР|АС|КЖ\d?|КМД?|КД|КР\d?|ОВиК|ОВ\d?|ВК|НВК|ЭОМ|ЭМ|ЭО|ЭС|ЭН|СС|ТХ|ГП|ГТ|ПОС|ПЗ|ТС|ТМ|АК|АОВ|ПБ|ОДИ|ИОС\d?|"
         r"ПЖ|ПТ|ПС|СКС|АПС|АУПТ|ГСН|ГСВ|НК|ЛК|КЗ|ПОД|ППР|ТК)")
RD_CODE = re.compile(rf"(?<![\w/.-])(\d[\w.]*(?:[/.-][\w]+)*[-–]({MARKS})(?:\d+)?(?:\.\d+)?)(?![\w])")
SHEETS = re.compile(r"\b(?:листы?|л\.)\s*(\d+(?:\s*[-–,]\s*\d+)*)", re.IGNORECASE)
REVISION = re.compile(r"\b(?:изм(?:енение)?\.?\s*(?:№\s*)?)(\d{1,2})\b", re.IGNORECASE)
AXES = re.compile(r"\b(?:в\s+)?ос(?:ях|и|ей)\s+((?:\d+[-–]\d+|\d+|[А-Я][-–][А-Я]|[А-Я])(?:\s*/\s*(?:\d+[-–]\d+|\d+|[А-Я][-–][А-Я]|[А-Я]))?)",
                  re.IGNORECASE)
AXES_BARE = re.compile(r"(?<![\w.,/])(\d+[-–]\d+\s*/\s*[А-Я][-–][А-Я]|[А-Я][-–][А-Я]\s*/\s*\d+[-–]\d+)(?![\w])")
LEVEL = re.compile(r"\bотм(?:етк\w*)?\.?\s*([-+−]?\s*\d+[.,]\d{3})", re.IGNORECASE)
CONCRETE = re.compile(r"(?<![\w])([BВ]\s?\d{1,2}(?:[.,]\d)?(?:\s*[WШ]\s?\d{1,2})?(?:\s*F\s?\d{2,3})?(?:\s*П\d)?)(?![\w])")
REBAR = re.compile(r"(?<![\w])([АA]\d{3}[СC]?)(?![\w])")
DIAM = re.compile(r"(?<![\w])(?:d|Ø|ø|ф)\s?(\d{1,2})(?![\w]|[.,]\d)")
GOST = re.compile(r"\b(ГОСТ\s*Р?\s*[\d.]+(?:[-–]\d{2,4})?)")
NORM = re.compile(r"(?<![\w-])((?:СП|СНиП|ТР ТС|ВСН|РД|МДС)\s*\d[\d.]*(?:[-–]\d{2,4})?(?:\s*(?:п\.|пункт)\s*\d[\d.]*)?)")
ELEMENT = re.compile(
    r"\b((?:фундаментн\w+\s+)?плит\w*(?:\s+перекрыти\w*|\s+покрыти\w*)?|колонн\w*|стен\w*|ростверк\w*|сва[ийяю]\w*|балк\w*|"
    r"ригел\w*|диафрагм\w*|пилон\w*|лестничн\w+\s+марш\w*|перемычк\w*|фундамент\w*|подготовк\w*|гидроизоляци\w*|"
    r"кладк\w*|утеплени\w*|кровл\w*|перекрыти\w*)(?:\s+([А-ЯA-Z][а-яa-z]?[-.]?\d+(?:[-.]\d+)?))?",
    re.IGNORECASE)
LETTER_TO = re.compile(r"\b(?:директору|руководителю|начальнику|главному инженеру|заместителю)\s+([^\n,]{3,80})", re.IGNORECASE)
ORG = re.compile(r"\b((?:ООО|АО|ПАО|ЗАО|ОАО|ИП|ГУП|МУП|ФГУП|ГКУ|ГБУ)\s+[«\"][^»\"]{2,60}[»\"])")
REPLY_TO = re.compile(r"\bна\s+(?:ваш[ее]?\s+)?(?:исх(?:одящ\w*)?\.?\s*|письм\w*\s+)?№\s*([\w\-/.]+)(?:\s+от\s+(\d{2}\.\d{2}\.\d{4}))?",
                      re.IGNORECASE)
CONTRACT_REF = re.compile(r"\b(?:по\s+)?договор\w*(?:\s+(?:поставки|подряда|субподряда|оказания\s+услуг))?\s*№\s*([\w\-/.]+)",
                          re.IGNORECASE)
ACT_REF = re.compile(r"\b(?:АОСР|АООК|акт\w*\s+освидетельствования[^№\n]{0,40})\s*№\s*([\w\-/.]+)", re.IGNORECASE)
_MORE = re.compile(r"(?:\s*,\s*|\s+и\s+)№\s*([\w\-/]+)")
NUMBER_BY_KIND = {
    "aosr": r"АКТ[^\n№]{0,120}№\s*([\w\-/.]+)", "aook": r"АКТ[^\n№]{0,120}№\s*([\w\-/.]+)",
    "letter": r"\bисх\.?\s*№\s*([\w\-/.]+)", "incoming_letter": r"\bисх\.?\s*№\s*([\w\-/.]+)",
    "contract": r"ДОГОВОР[^\n№]{0,60}№\s*([\w\-/.]+)", "order": r"ПРИКАЗ[^\n№]{0,40}№\s*([\w\-/.]+)",
    "certificate": r"(?:сертификат|паспорт|документ о качестве|декларац)\w*[^\n№]{0,40}№\s*([\w\-/.]+)",
    "test_protocol": r"протокол\w*[^\n№]{0,40}№\s*([\w\-/.]+)", "scheme": r"схем\w*[^\n№]{0,40}№\s*([\w\-/.]+)",
    "ks2": r"№\s*([\w\-/.]+)", "ks3": r"№\s*([\w\-/.]+)",
}
_DMY = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{4})")


def _iso(d: str | None) -> str | None:
    if not d:
        return None
    m = _DMY.search(d)
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def _uniq(items, limit: int = 12) -> list[str]:
    out: list[str] = []
    for x in items:
        x = re.sub(r"\s+", " ", str(x)).strip(" .,;")
        if x and x.lower() not in {o.lower() for o in out}:
            out.append(x)
        if len(out) >= limit:
            break
    return out


def rules_card(text: str, kind: str, name: str = "") -> Card:
    from bis.services.archive import doc_date
    from bis.services.id_check import references

    head = text[:2500]
    c = Card(kind=kind)
    m = re.search(NUMBER_BY_KIND.get(kind, r"№\s*([\w\-/.]+)"), head, re.IGNORECASE)
    if m:
        c.number = m.group(1).rstrip(".,")
    d = doc_date(text, kind)
    c.date = d.isoformat() if d else None
    codes = RD_CODE.findall(text)
    if codes:
        c.rd_code, c.rd_mark = codes[0][0], codes[0][1]
    c.sheets = _uniq((s.replace(" ", "") for s in SHEETS.findall(text)), 8)
    rev = REVISION.search(text)
    c.revision = rev.group(1) if rev else None
    c.axes = _uniq([a.replace(" ", "") for a in AXES.findall(text)] + [a.replace(" ", "") for a in AXES_BARE.findall(text)], 10)
    c.levels = _uniq((lv.replace(" ", "").replace(",", ".").replace("−", "-") for lv in LEVEL.findall(text)), 10)
    grades = [g.replace(" ", " ") for g in CONCRETE.findall(text) if re.search(r"\d", g)]
    grades += [r.translate(_LATIN) for r in REBAR.findall(text)] + [f"d{x}" for x in DIAM.findall(text)] + GOST.findall(text)
    c.grades = _uniq((g.translate(_LATIN) if g[:1] in "BA" else g for g in grades), 15)
    c.norms = _uniq(NORM.findall(text), 10)
    c.elements = _uniq((" ".join(x for x in e if x) for e in ELEMENT.findall(head)), 6)
    refs = [DocRef(kind=r.kind, number=r.number, label=r.title) for r in references(text)]
    for num, date in REPLY_TO.findall(text):
        refs.append(DocRef(kind="letter", number=num.rstrip(".,"), date=_iso(date), label="письмо"))
    if kind != "contract":
        refs += [DocRef(kind="contract", number=n.rstrip(".,"), label="договор") for n in CONTRACT_REF.findall(text)]
    if kind not in ("aosr", "aook"):
        for m in ACT_REF.finditer(text):  # «АОСР № 1, № 2 и № 3»
            refs.append(DocRef(kind="act", number=m.group(1).rstrip(".,"), label="акт"))
            pos = m.end()
            while more := _MORE.match(text, pos):
                refs.append(DocRef(kind="act", number=more.group(1).rstrip(".,"), label="акт"))
                pos = more.end()
    own = {"certificate": "quality", "test_protocol": "protocol", "scheme": "scheme", "letter": "letter",
           "incoming_letter": "letter", "contract": "contract", "aosr": "act", "aook": "act"}.get(kind)
    seen: set[tuple[str, str]] = set()
    c.refs = [r for r in refs if (r.kind, r.number.lower()) not in seen and not seen.add((r.kind, r.number.lower()))
              and not (r.kind == own and c.number and r.number.lower() == c.number.lower())][:30]  # не ссылка на себя
    orgs = _uniq(ORG.findall(head), 6)
    if kind == "contract":  # стороны договора — ещё и в реквизитах в конце
        tail = text[-4000:]
        orgs = _uniq([*orgs, *ORG.findall(tail)], 6)
    c.parties = orgs
    if kind in ("letter", "incoming_letter") and orgs:
        c.sender = orgs[0]
        to = LETTER_TO.search(head)
        c.recipient = to.group(1).strip() if to else (orgs[1] if len(orgs) > 1 else None)
    m = re.search(r"начал\w*\s+(?:работ\w*\s+)?[^0-9]{0,20}(\d{1,2}\.\d{1,2}\.\d{4})", text, re.IGNORECASE)
    c.work_start = _iso(m.group(1)) if m else None
    m = re.search(r"окончани\w*\s+(?:работ\w*\s+)?[^0-9]{0,20}(\d{1,2}\.\d{1,2}\.\d{4})", text, re.IGNORECASE)
    c.work_end = _iso(m.group(1)) if m else None
    m = re.search(r"\b(?:в\s+срок\s+до|не\s+позднее|до)\s+(\d{1,2}\.\d{1,2}\.\d{4})", text, re.IGNORECASE)
    c.deadline = _iso(m.group(1)) if m else None
    return c


# --- модель --------------------------------------------------------------------------------------


class ModelCard(BaseModel):
    summary: str | None = Field(None, description="Суть документа одной фразой до 25 слов: что, по какой конструкции, итог")
    elements: list[str] = Field(default_factory=list, description="Конструкции с марками: «фундаментная плита Фм1»")
    works: list[str] = Field(default_factory=list, description="Виды работ: «армирование», «бетонирование»")
    materials: list[MaterialRef] = Field(default_factory=list)
    sender: str | None = Field(None, description="Отправитель (для писем) — организация")
    recipient: str | None = Field(None, description="Получатель (для писем) — организация или должность")
    deadline: str | None = Field(None, description="Срок, если он требуется документом: ДД.ММ.ГГГГ")


INSTRUCTIONS = ("Ты разбираешь строительный документ для «карточки». Верни только то, что прямо написано в тексте. "
                "summary — суть одной фразой (например: «Освидетельствование армирования фундаментной плиты Фм1 в осях "
                "1-6/А-Г»; для письма — что просят и к какому сроку). Конструкции — с марками, как в тексте. "
                "Материалы — с марками/классами и документами о качестве, если указаны. Для договора summary — "
                "стороны, предмет, цена и срок, если они есть в тексте.")


def _merge_elements(model: list[str], rules: list[str]) -> list[str]:
    """Конструкции от модели (в именительном падеже) + из правил только те марки, которых модель не назвала."""
    if not model:
        return rules
    marks = {m.lower() for e in model for m in re.findall(r"[А-ЯA-Z][а-яa-z]?[-.]?\d+(?:[-.]\d+)?", e)}
    extra = [e for e in rules if (found := re.findall(r"[А-ЯA-Z][а-яa-z]?[-.]?\d+(?:[-.]\d+)?", e))
             and not {f.lower() for f in found} & marks]
    return _uniq([*model, *extra], 8)


def _fallback_summary(text: str, kind: str, card: Card) -> str | None:
    """Суть без модели: для договора — стороны и предмет, иначе первая содержательная строка (не город и не дата)."""
    if kind == "contract":
        m = re.search(r"(?:предмет\w*\s+договора[^\n]*\n+)?([^\n]{0,40}обязу\w+[^\n]{20,300})", text, re.IGNORECASE)
        about = re.sub(r"^\s*[\d.]+\s*", "", m.group(1)).strip() if m else ""
        who = " и ".join(card.parties[:2])
        out = "; ".join(x for x in (who, about) if x)
        if out:
            return out[:300]
    first = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) > 25 and "№" not in ln
                  and not re.match(r"\s*г\.\s", ln)), "")
    return first[:200] or None


def build_card(gateway, text: str, kind: str, name: str = "") -> Card:
    """Карточка: правила + модель (если доступна). Ошибка модели не мешает — остаётся карточка по правилам."""
    card = rules_card(text, kind, name)
    card.text_chars = len(text)
    if gateway is not None and text.strip():
        try:
            from bis.llm.extract import extract

            res = extract(gateway, ModelCard, text, instructions=INSTRUCTIONS, task="extract")
            m = res.data
            if m is not None and res.provider not in (None, "offline"):
                card.model_read_complete = res.complete
                card.summary = (m.summary or "").strip()[:300] or None
                card.elements = _merge_elements(m.elements, card.elements)
                card.works = _uniq(m.works, 8)
                card.materials = m.materials[:12]
                card.sender = card.sender or m.sender
                card.recipient = card.recipient or m.recipient
                card.deadline = card.deadline or _iso(m.deadline)
                card.by = "rules+model"
        except Exception:  # noqa: BLE001 — модель недоступна: карточка по правилам
            pass
    if not card.summary:
        card.summary = _fallback_summary(text, kind, card)
    card.title = card.label()
    return card
