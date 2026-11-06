"""Извлечение текста из файлов: PDF, DOCX, XLSX, TXT/MD, EML, HTML."""

from __future__ import annotations

import email
import email.policy
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Page:
    number: int | None
    text: str


@dataclass
class ParsedDocument:
    pages: list[Page]
    title: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)


class UnsupportedFormat(ValueError):
    pass


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251", "koi8-r"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", html)
    html = re.sub(r"<[^>]+>", " ", html)
    import html as html_lib

    return re.sub(r"[ \t]+", " ", html_lib.unescape(html))


def parse_pdf(data: bytes) -> ParsedDocument:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [Page(i + 1, (p.extract_text() or "")) for i, p in enumerate(reader.pages)]
    chars = sum(len(p.text.strip()) for p in pages)
    meta: dict[str, Any] = {"pages": len(pages)}
    if pages and chars / len(pages) < 50:
        meta["needs_ocr"] = True  # скан без текстового слоя
    title = None
    if reader.metadata and reader.metadata.title:
        title = str(reader.metadata.title)
    return ParsedDocument(pages=pages, title=title, meta=meta)


def parse_docx(data: bytes) -> ParsedDocument:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(io.BytesIO(data))
    lines: list[str] = []
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, d).text
            if text.strip():
                lines.append(text)
        elif tag == "tbl":
            for row in Table(child, d).rows:
                cells: list[str] = []
                for c in row.cells:
                    t = c.text.strip()
                    if t and (not cells or cells[-1] != t):  # объединённые ячейки повторяются
                        cells.append(t)
                if cells:
                    lines.append(" | ".join(cells))
    title = d.core_properties.title or None
    return ParsedDocument(pages=[Page(None, "\n".join(lines))], title=title)


def parse_xlsx(data: bytes) -> ParsedDocument:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    pages: list[Page] = []
    for ws in wb.worksheets:
        lines = [f"Лист: {ws.title}"]
        for row in ws.iter_rows(values_only=True):
            cells = [str(v).strip() for v in row if v is not None and str(v).strip()]
            if cells:
                lines.append(" | ".join(cells))
        pages.append(Page(None, "\n".join(lines)))
    return ParsedDocument(pages=pages)


def parse_eml(data: bytes) -> ParsedDocument:
    msg = email.message_from_bytes(data, policy=email.policy.default)
    headers = {k: str(msg.get(k, "")) for k in ("From", "To", "Cc", "Subject", "Date")}
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        text = body.get_content()
        if body.get_content_type() == "text/html":
            text = _strip_html(text)
    attachments = [part.get_filename() for part in msg.iter_attachments() if part.get_filename()]
    head = "\n".join(f"{k}: {v}" for k, v in headers.items() if v)
    if attachments:
        head += "\nВложения: " + ", ".join(attachments)
    return ParsedDocument(
        pages=[Page(None, head + "\n\n" + text)],
        title=headers.get("Subject") or None,
        meta={"email": headers, "attachments": attachments},
    )


def parse_file(data: bytes, filename: str, mime: str | None = None) -> ParsedDocument:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf" or mime == "application/pdf":
        return parse_pdf(data)
    if ext == ".docx":
        return parse_docx(data)
    if ext in (".xlsx", ".xlsm"):
        return parse_xlsx(data)
    if ext == ".eml" or mime == "message/rfc822":
        return parse_eml(data)
    if ext in (".html", ".htm"):
        return ParsedDocument(pages=[Page(None, _strip_html(_decode(data)))])
    if ext in (".txt", ".md", ".csv", "") or (mime or "").startswith("text/"):
        return ParsedDocument(pages=[Page(None, _decode(data))])
    raise UnsupportedFormat(f"Формат {ext or mime} пока не поддерживается (DOC/RTF — конвертируйте в DOCX/PDF)")
