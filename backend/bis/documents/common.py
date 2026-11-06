"""Общие модели и утилиты оформления документов (python-docx)."""

from __future__ import annotations

import io
from datetime import date

from docx import Document as DocxDocument
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from pydantic import BaseModel

MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def fmt_date_long(d: date | None) -> str:
    if not d:
        return "«___» ____________ 20__ г."
    return f"«{d.day:02d}» {MONTHS_GEN[d.month - 1]} {d.year} г."


def fmt_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "__.__.____"


def short_name(full_name: str) -> str:
    """«Петров Пётр Петрович» → «Петров П.П.»"""
    parts = full_name.split()
    if len(parts) < 2 or any(p.endswith(".") for p in parts[1:]):
        return full_name
    return parts[0] + " " + "".join(p[0] + "." for p in parts[1:3])


class Party(BaseModel):
    """Юридическое лицо или ИП — участник строительства."""

    name: str
    ogrn: str | None = None
    inn: str | None = None
    kpp: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    sro: str | None = None

    def format(self) -> str:
        parts = [self.name]
        if self.ogrn:
            parts.append(f"ОГРН {self.ogrn}")
        if self.inn:
            parts.append(f"ИНН {self.inn}")
        if self.address:
            parts.append(self.address)
        if self.phone:
            parts.append(f"тел. {self.phone}")
        if self.sro:
            parts.append(f"СРО: {self.sro}")
        return ", ".join(parts)


# --- оформление -------------------------------------------------------------------------------


def new_document(font_size: float = 12, margins_cm: tuple[float, float, float, float] = (2.0, 1.5, 2.0, 1.5)):
    """margins: (left, right, top, bottom)."""
    doc = DocxDocument()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(font_size)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), "Times New Roman")
    pf = style.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    for section in doc.sections:
        section.page_height, section.page_width = Cm(29.7), Cm(21.0)
        section.left_margin, section.right_margin = Cm(margins_cm[0]), Cm(margins_cm[1])
        section.top_margin, section.bottom_margin = Cm(margins_cm[2]), Cm(margins_cm[3])
    return doc


def para(doc, text: str = "", *, bold=False, italic=False, size: float | None = None, align=None,
         underline=False, indent_cm: float | None = None, space_after: float = 0):
    p = doc.add_paragraph()
    if text:
        run = p.add_run(text)
        run.bold, run.italic, run.underline = bold, italic, underline
        if size:
            run.font.size = Pt(size)
    if align:
        p.alignment = align
    if indent_cm is not None:
        p.paragraph_format.first_line_indent = Cm(indent_cm)
    p.paragraph_format.space_after = Pt(space_after)
    return p


def field_line(doc, label: str | None, value: str, caption: str | None = None, *, size: float = 11):
    """Поле формы: «Метка: значение» с подчёркнутым значением и мелкой подписью-пояснением."""
    p = doc.add_paragraph()
    if label:
        r = p.add_run(label + " ")
        r.font.size = Pt(size)
    r = p.add_run(value or " " * 40)
    r.underline = True
    r.font.size = Pt(size)
    if caption:
        c = doc.add_paragraph()
        cr = c.add_run(f"({caption})")
        cr.font.size = Pt(7.5)
        cr.italic = True
        c.alignment = WD_ALIGN_PARAGRAPH.CENTER
    return p


def borderless_table(doc, rows: int, cols: int, widths_cm: list[float]):
    table = doc.add_table(rows=rows, cols=cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    for row in table.rows:
        for i, w in enumerate(widths_cm):
            row.cells[i].width = Cm(w)
    return table


def bottom_border(paragraph) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "8"), ("w:space", "1"), ("w:color", "000000")):
        bottom.set(qn(k), v)
    borders.append(bottom)
    ppr.append(borders)


def to_bytes(doc) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def docx_text(data: bytes) -> str:
    """Весь текст DOCX (абзацы и таблицы) — для индексации и проверок."""
    import docx

    d = docx.Document(io.BytesIO(data))
    lines = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            for cell in row.cells:
                lines.append(cell.text)
    return "\n".join(x for x in lines if x.strip())
