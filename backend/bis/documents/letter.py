"""Деловое (исходящее) письмо на бланке организации. Реквизиты — по ГОСТ Р 7.0.97-2016."""

from __future__ import annotations

import datetime as dt

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from pydantic import BaseModel, Field

from bis.documents.common import (
    Party,
    borderless_table,
    bottom_border,
    fmt_date,
    new_document,
    para,
    to_bytes,
)


class LetterData(BaseModel):
    sender: Party
    number: str | None = None
    date: dt.date | None = None
    in_reply_to: str | None = Field(None, description="«на № 15 от 01.03.2026»")
    addressee: list[str] = Field(description="Строки адресата в дательном падеже: должность, организация, ФИО")
    subject: str = Field(description="Заголовок к тексту: «О направлении исполнительной документации»")
    greeting: str | None = Field(None, description="«Уважаемый Иван Иванович!»")
    body: list[str] = Field(description="Абзацы текста письма")
    attachments: list[str] = Field(default_factory=list)
    signer_position: str
    signer_name: str
    executor: str | None = Field(None, description="Исполнитель: ФИО, телефон")


def render_letter(d: LetterData) -> bytes:
    doc = new_document(font_size=12, margins_cm=(2.0, 1.0, 2.0, 2.0))
    s = d.sender
    para(doc, s.name, bold=True, size=13, align=WD_ALIGN_PARAGRAPH.CENTER)
    req = [x for x in (s.address, f"тел. {s.phone}" if s.phone else None, s.email) if x]
    ids = [x for x in (f"ИНН {s.inn}" if s.inn else None, f"КПП {s.kpp}" if s.kpp else None,
                       f"ОГРН {s.ogrn}" if s.ogrn else None) if x]
    if req:
        para(doc, ", ".join(req), size=9, align=WD_ALIGN_PARAGRAPH.CENTER)
    p = para(doc, ", ".join(ids), size=9, align=WD_ALIGN_PARAGRAPH.CENTER)
    bottom_border(p)
    para(doc)

    t = borderless_table(doc, 1, 2, [8.5, 8.5])
    left = t.cell(0, 0)
    left.text = f"{fmt_date(d.date)} № {d.number or '_____'}"
    if d.in_reply_to:
        left.add_paragraph(d.in_reply_to if d.in_reply_to.lower().startswith("на") else f"на № {d.in_reply_to}")
    right = t.cell(0, 1)
    right.text = d.addressee[0] if d.addressee else ""
    for line in d.addressee[1:]:
        right.add_paragraph(line)
    para(doc)

    para(doc, d.subject, space_after=12)
    if d.greeting:
        para(doc, d.greeting, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=6)
    for text in d.body:
        para(doc, text, align=WD_ALIGN_PARAGRAPH.JUSTIFY, indent_cm=1.25)
    if d.attachments:
        para(doc)
        if len(d.attachments) == 1:
            para(doc, f"Приложение: {d.attachments[0]}")
        else:
            para(doc, "Приложение:")
            for i, a in enumerate(d.attachments, 1):
                para(doc, f"{i}. {a}", indent_cm=1.25)
    para(doc)
    para(doc)
    sig = borderless_table(doc, 1, 2, [11.0, 6.0])
    sig.cell(0, 0).text = d.signer_position
    sig.cell(0, 1).text = d.signer_name
    sig.cell(0, 1).paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    if d.executor:
        para(doc)
        para(doc)
        p = para(doc, f"Исп. {d.executor}")
        for r in p.runs:
            r.font.size = Pt(9)
    return to_bytes(doc)
