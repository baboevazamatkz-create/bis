"""Акт освидетельствования скрытых работ (АОСР).

Структура формы — по составу и порядку ведения исполнительной документации
(Приказ Минстроя России от 16.05.2023 № 344/пр). Форма хранится в коде как построитель DOCX;
организация может загрузить собственный шаблон (см. bis.documents.templates).
"""

from __future__ import annotations

import datetime as dt
import re
from datetime import date

from docx.enum.text import WD_ALIGN_PARAGRAPH
from pydantic import BaseModel, Field

from bis.documents.common import (
    Party,
    borderless_table,
    field_line,
    fmt_date_long,
    new_document,
    para,
    short_name,
    to_bytes,
)

REPRESENTATIVE_ROLES: dict[str, dict] = {
    "developer_control": {
        "title": "Представитель застройщика или технического заказчика по вопросам строительного контроля",
        "caption": "должность, фамилия, инициалы, идентификационный номер в национальном реестре специалистов "
        "в области строительства, реквизиты распорядительного документа, подтверждающего полномочия",
        "nrs": True,
        "required": True,
    },
    "builder": {
        "title": "Представитель лица, осуществляющего строительство",
        "caption": "должность, фамилия, инициалы, реквизиты распорядительного документа, подтверждающего полномочия",
        "nrs": False,
        "required": True,
    },
    "builder_control": {
        "title": "Представитель лица, осуществляющего строительство, по вопросам строительного контроля "
        "(специалист по организации строительства)",
        "caption": "должность, фамилия, инициалы, идентификационный номер в национальном реестре специалистов "
        "в области строительства, реквизиты распорядительного документа, подтверждающего полномочия",
        "nrs": True,
        "required": True,
    },
    "designer": {
        "title": "Представитель лица, осуществляющего подготовку проектной документации",
        "caption": "должность, фамилия, инициалы, реквизиты распорядительного документа, подтверждающего полномочия",
        "nrs": False,
        "required": False,
    },
    "performer": {
        "title": "Представитель лица, выполнившего работы, подлежащие освидетельствованию",
        "caption": "должность, фамилия, инициалы, реквизиты распорядительного документа, подтверждающего полномочия",
        "nrs": False,
        "required": False,
    },
}

_QUALITY_DOC_RE = re.compile(r"сертифик|паспорт|деклараци|протокол|свидетельств|документ о качестве", re.IGNORECASE)


class Representative(BaseModel):
    position: str
    full_name: str
    nrs_id: str | None = None
    order: str | None = Field(None, description="Реквизиты распорядительного документа, например «приказ № 12 от 10.01.2026»")
    organization: str | None = None

    def format(self, with_nrs: bool = True) -> str:
        parts = [f"{self.position} {short_name(self.full_name)}"]
        if self.organization:
            parts.append(self.organization)
        if with_nrs and self.nrs_id:
            parts.append(f"идентификационный номер в НРС {self.nrs_id}")
        if self.order:
            parts.append(self.order)
        return ", ".join(parts)


class AOSRData(BaseModel):
    number: str | None = None
    date: dt.date | None = None
    object_name: str = Field(description="Наименование объекта по ПД и адрес")
    developer: Party = Field(description="Застройщик или технический заказчик")
    builder: Party = Field(description="Лицо, осуществляющее строительство")
    designer: Party | None = Field(None, description="Лицо, осуществляющее подготовку ПД")
    performer: Party | None = Field(None, description="Лицо, выполнившее работы (если не генподрядчик)")
    representatives: dict[str, Representative] = Field(default_factory=dict)
    other_representatives: list[Representative] = Field(default_factory=list)
    works: str = Field(description="Работы, предъявленные к освидетельствованию")
    project_docs: str = Field(description="Шифр/листы рабочей документации, наименование, разработчик")
    materials: list[str] = Field(default_factory=list, description="Материалы со ссылками на документы о качестве")
    executive_docs: list[str] = Field(default_factory=list, description="Исполнительные схемы, протоколы испытаний")
    work_start: dt.date | None = None
    work_end: dt.date | None = None
    norms: list[str] = Field(default_factory=list, description="Нормы и разделы ПД, которым соответствуют работы")
    next_works: str | None = Field(None, description="Разрешается производство последующих работ по…")
    additional_info: str | None = None
    copies: int = 3
    attachments: list[str] = Field(default_factory=list)


class Issue(BaseModel):
    level: str  # error | warning
    field: str
    message: str


def validate_aosr(d: AOSRData) -> list[Issue]:
    """Детерминированная проверка полноты и непротиворечивости акта (без LLM)."""
    issues: list[Issue] = []

    def err(f: str, m: str) -> None:
        issues.append(Issue(level="error", field=f, message=m))

    def warn(f: str, m: str) -> None:
        issues.append(Issue(level="warning", field=f, message=m))

    if not d.works.strip():
        err("works", "Не указаны работы, предъявленные к освидетельствованию")
    if not d.project_docs.strip():
        err("project_docs", "Не указана проектная/рабочая документация")
    if not d.work_start or not d.work_end:
        err("work_start", "Не указаны даты начала и окончания работ")
    elif d.work_end < d.work_start:
        err("work_end", "Дата окончания работ раньше даты начала")
    if d.date and d.work_end and d.date < d.work_end:
        err("date", "Дата акта раньше даты окончания работ")
    if d.date and d.date > date.today():
        warn("date", "Дата акта в будущем")
    for role, meta in REPRESENTATIVE_ROLES.items():
        rep = d.representatives.get(role)
        if rep is None:
            if meta["required"]:
                err(f"representatives.{role}", f"Не указан: {meta['title']}")
            continue
        if meta["nrs"] and not rep.nrs_id:
            err(f"representatives.{role}.nrs_id", f"Нет идентификационного номера НРС: {rep.full_name}")
        if not rep.order:
            warn(f"representatives.{role}.order", f"Нет реквизитов распорядительного документа: {rep.full_name}")
    if d.designer and "designer" not in d.representatives:
        warn("representatives.designer", "Указан проектировщик, но нет его представителя (при авторском надзоре обязателен)")
    if not d.materials:
        warn("materials", "Не указаны применённые материалы")
    for i, m in enumerate(d.materials):
        if not _QUALITY_DOC_RE.search(m):
            warn(f"materials.{i}", f"Нет ссылки на документ о качестве: «{m}»")
    if not d.executive_docs:
        warn("executive_docs", "Не приложены исполнительные схемы / результаты испытаний")
    if not d.norms:
        warn("norms", "Не указаны нормативы и разделы ПД, которым соответствуют работы")
    if not d.next_works:
        warn("next_works", "Не указаны последующие работы")
    return issues


def act_header(doc, d, subtitle: str,
               performer_label: str = "Лицо, выполнившее работы, подлежащие освидетельствованию") -> None:
    """Общая шапка актов ИД (344/пр): объект, участники, заголовок, представители."""
    field_line(doc, "Объект капитального строительства", d.object_name,
               "наименование объекта капитального строительства в соответствии с проектной документацией, "
               "почтовый или строительный адрес объекта капитального строительства")
    party_caption = ("наименование, ОГРН, ИНН, место нахождения, телефон; наименование, ОГРН, ИНН "
                     "саморегулируемой организации, членом которой является")
    field_line(doc, "Застройщик или технический заказчик", d.developer.format(), party_caption)
    field_line(doc, "Лицо, осуществляющее строительство", d.builder.format(), party_caption)
    if d.designer:
        field_line(doc, "Лицо, осуществляющее подготовку проектной документации", d.designer.format(), party_caption)
    field_line(doc, performer_label,
               (d.performer or d.builder).format(), party_caption)

    para(doc)
    para(doc, "АКТ", bold=True, size=13, align=WD_ALIGN_PARAGRAPH.CENTER)
    para(doc, subtitle, bold=True, size=12, align=WD_ALIGN_PARAGRAPH.CENTER)
    t = borderless_table(doc, 1, 2, [8.5, 9.0])
    t.cell(0, 0).text = f"№ {d.number or '______'}"
    t.cell(0, 1).text = fmt_date_long(d.date)
    t.cell(0, 1).paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    para(doc)

    for role, meta in REPRESENTATIVE_ROLES.items():
        rep = d.representatives.get(role)
        if rep is None and not meta["required"]:
            continue
        field_line(doc, meta["title"], rep.format(meta["nrs"]) if rep else "", meta["caption"])
    if d.other_representatives:
        para(doc, "а также иные представители лиц, участвующих в освидетельствовании:")
        for rep in d.other_representatives:
            field_line(doc, None, rep.format(), "наименование, должность, фамилия, инициалы, реквизиты распорядительного документа")



def render_aosr(d: AOSRData) -> bytes:
    doc = new_document(font_size=11, margins_cm=(2.5, 1.0, 1.5, 1.5))
    act_header(doc, d, "освидетельствования скрытых работ")
    performer = (d.performer or d.builder).name
    field_line(doc, "произвели осмотр работ, выполненных", performer, "наименование лица, выполнившего работы")
    para(doc, "и составили настоящий акт о нижеследующем:", space_after=4)

    items = [
        ("1. К освидетельствованию предъявлены следующие работы:", d.works, "наименование скрытых работ"),
        ("2. Работы выполнены по проектной документации", d.project_docs,
         "номер, другие реквизиты чертежа, наименование проектной и (или) рабочей документации, "
         "сведения о лицах, осуществляющих подготовку раздела проектной и (или) рабочей документации"),
        ("3. При выполнении работ применены", "; ".join(d.materials),
         "наименование строительных материалов (изделий) со ссылкой на сертификаты или другие документы, "
         "подтверждающие их качество и безопасность"),
        ("4. Предъявлены документы, подтверждающие соответствие работ предъявляемым к ним требованиям:",
         "; ".join(d.executive_docs),
         "исполнительные схемы и чертежи, результаты экспертиз, обследований, лабораторных и иных испытаний "
         "выполненных работ, проведенных в процессе строительного контроля"),
    ]
    for label, value, caption in items:
        field_line(doc, label, value, caption)
    para(doc, "5. Даты:")
    field_line(doc, "начала работ", fmt_date_long(d.work_start))
    field_line(doc, "окончания работ", fmt_date_long(d.work_end))
    field_line(doc, "6. Работы выполнены в соответствии с", "; ".join(d.norms),
               "наименования и структурные единицы технических регламентов, иных нормативных правовых актов, "
               "разделы проектной и (или) рабочей документации")
    field_line(doc, "7. Разрешается производство последующих работ по", d.next_works or "",
               "наименование работ, конструкций, участков сетей инженерно-технического обеспечения")
    para(doc)
    field_line(doc, "Дополнительные сведения", d.additional_info or "")
    para(doc, f"Акт составлен в {d.copies} экземплярах.")
    if d.attachments:
        para(doc, "Приложения:")
        for i, a in enumerate(d.attachments, 1):
            para(doc, f"{i}. {a}")

    act_signatures(doc, d)
    return to_bytes(doc)


def act_signatures(doc, d) -> None:
    """Подписи представителей (общие для АОСР и АООК)."""
    para(doc)
    for role, meta in REPRESENTATIVE_ROLES.items():
        rep = d.representatives.get(role)
        if rep is None:
            continue
        para(doc, meta["title"], size=9, italic=True)
        sig = borderless_table(doc, 1, 2, [11.5, 6.0])
        sig.cell(0, 0).text = f"{rep.position} {short_name(rep.full_name)}"
        sig.cell(0, 1).text = "_______________ (подпись)"
        para(doc, space_after=4)
    for rep in d.other_representatives:
        sig = borderless_table(doc, 1, 2, [11.5, 6.0])
        sig.cell(0, 0).text = f"{rep.position} {short_name(rep.full_name)}"
        sig.cell(0, 1).text = "_______________ (подпись)"
