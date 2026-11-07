"""Акт освидетельствования ответственных конструкций (АООК) — по составу ИД (Приказ Минстроя России № 344/пр).

Участники, представители и подписи — те же, что в АОСР. Пункты акта: предъявленные конструкции, проектная
документация, материалы, документы о соответствии (в т.ч. АОСР на входящие скрытые работы), испытания, даты,
нормы, решение об использовании конструкций.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from bis.documents.aosr import AOSRData, Issue, act_header, act_signatures, validate_aosr
from bis.documents.common import field_line, fmt_date_long, new_document, para, to_bytes

PERMITS = {
    "full": "разрешается использование конструкций по назначению",
    "partial": "разрешается использование конструкций по назначению с нагружением в размере {load}% проектной нагрузки",
    "conditional": "разрешается полное нагружение при выполнении следующих условий: {conditions}",
}


class AOOKData(AOSRData):
    works: str = Field(description="Ответственные конструкции, предъявленные к освидетельствованию")
    tests: list[str] = Field(default_factory=list, description="Проведённые испытания и опробования")
    aosr_refs: list[str] = Field(default_factory=list, description="АОСР на скрытые работы в составе конструкций")
    permit: Literal["full", "partial", "conditional"] = "full"
    load_percent: int | None = None
    conditions: str | None = None


def validate_aook(d: AOOKData) -> list[Issue]:
    issues = [i for i in validate_aosr(d) if i.field != "next_works"]
    if not d.aosr_refs:
        issues.append(Issue(level="warning", field="aosr_refs",
                            message="Не указаны АОСР на скрытые работы, входящие в состав конструкций"))
    if not d.tests:
        issues.append(Issue(level="warning", field="tests", message="Не указаны испытания (прочность бетона, сварные швы и т.п.)"))
    if d.permit == "partial" and not d.load_percent:
        issues.append(Issue(level="error", field="load_percent", message="Для частичного нагружения укажите процент нагрузки"))
    if d.permit == "conditional" and not d.conditions:
        issues.append(Issue(level="error", field="conditions", message="Укажите условия полного нагружения"))
    return issues


def render_aook(d: AOOKData) -> bytes:
    doc = new_document(font_size=11, margins_cm=(2.5, 1.0, 1.5, 1.5))
    act_header(doc, d, "освидетельствования ответственных конструкций",
               performer_label="Лицо, выполнившее конструкции, подлежащие освидетельствованию")
    field_line(doc, "произвели осмотр конструкций, выполненных", (d.performer or d.builder).name,
               "наименование лица, выполнившего конструкции")
    para(doc, "и составили настоящий акт о нижеследующем:", space_after=4)
    items = [
        ("1. К освидетельствованию предъявлены следующие ответственные конструкции:", d.works,
         "наименование и краткая характеристика конструкций"),
        ("2. Конструкции выполнены по проектной документации", d.project_docs,
         "номер, другие реквизиты чертежа, наименование проектной и (или) рабочей документации"),
        ("3. При выполнении конструкций применены", "; ".join(d.materials),
         "наименование строительных материалов (изделий) со ссылкой на документы о качестве"),
        ("4. Предъявлены документы, подтверждающие соответствие конструкций предъявляемым к ним требованиям:",
         "; ".join([*d.executive_docs, *d.aosr_refs]),
         "исполнительные схемы и чертежи, акты освидетельствования скрытых работ, результаты экспертиз и испытаний"),
        ("5. Проведены необходимые испытания и опробования:", "; ".join(d.tests), "наименование испытаний, номер и дата документа"),
    ]
    for label, value, caption in items:
        field_line(doc, label, value, caption)
    para(doc, "6. Даты:")
    field_line(doc, "начала работ", fmt_date_long(d.work_start))
    field_line(doc, "окончания работ", fmt_date_long(d.work_end))
    field_line(doc, "7. Конструкции выполнены в соответствии с", "; ".join(d.norms),
               "наименования и структурные единицы технических регламентов, иных нормативных правовых актов, разделы ПД")
    para(doc, "8. На основании изложенного:")
    permit = PERMITS[d.permit].format(load=d.load_percent or "__", conditions=d.conditions or "")
    para(doc, f"а) {permit};", indent_cm=1.0)
    if d.next_works:
        para(doc, f"б) разрешается производство последующих работ: {d.next_works}.", indent_cm=1.0)
    para(doc)
    field_line(doc, "Дополнительные сведения", d.additional_info or "")
    para(doc, f"Акт составлен в {d.copies} экземплярах.")
    if d.attachments:
        para(doc, "Приложения:")
        for i, a in enumerate(d.attachments, 1):
            para(doc, f"{i}. {a}")
    para(doc)
    act_signatures(doc, d)
    return to_bytes(doc)

