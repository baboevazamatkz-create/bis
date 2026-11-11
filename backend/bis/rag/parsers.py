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
    warning: str | None = None


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


def text_quality(text: str) -> float:
    """Rank extraction candidates; replacement/private-use glyphs are not readable text."""
    if not text.strip():
        return 0.0
    visible = [c for c in text if not c.isspace()]
    bad = sum(c == '\ufffd' or '\ue000' <= c <= '\uf8ff' or (ord(c) < 32 and c not in '\n\r\t') for c in visible)
    bad += sum(len(m.group()) for m in re.finditer(r'\(cid:\d+\)', text, re.I))
    useful = sum(c.isalnum() for c in visible)
    return max(0.0, useful - bad * 12) / max(1, len(visible))


def _decode(data: bytes) -> str:
    if data.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        return data.decode('utf-32')
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
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
    pages = []
    fallback = None
    methods = {}
    try:
        for i, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 — another engine can still extract this page
                text = ""
            method = 'pypdf'
            if len(text.strip()) < 50 or text_quality(text) < 0.5:
                try:
                    import pypdfium2 as pdfium

                    if fallback is None:
                        fallback = pdfium.PdfDocument(data)
                    with fallback[i] as rendered, rendered.get_textpage() as textpage:
                        candidate = textpage.get_text_range()
                    if (text_quality(candidate), len(candidate.strip())) > (text_quality(text), len(text.strip())):
                        text, method = candidate, 'pdfium'
                except Exception:  # noqa: BLE001 — preserve native text and report unread pages
                    pass
            if text_quality(text) < 0.2:
                text = ""
            pages.append(Page(i + 1, text))
            methods[str(i + 1)] = method
    finally:
        if fallback is not None:
            fallback.close()
    meta: dict[str, Any] = {"pages": len(pages)}
    meta['page_extractors'] = methods
    missing = [p.number for p in pages if len(p.text.strip()) < 50 or text_quality(p.text) < 0.5]
    if missing:
        meta["needs_ocr"] = True  # includes mixed PDFs with scanned appendices
        meta["ocr_pages"] = missing
    meta['unread_pages'] = [p.number for p in pages if not p.text.strip()]
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

    def cell_text(cell):
        content = []
        for child in cell._tc.iterchildren():
            tag = child.tag.rsplit('}', 1)[-1]
            if tag == 'p':
                content.append(Paragraph(child, cell).text)
            elif tag == 'tbl':
                content.extend(' | '.join(cell_text(c) for c in row.cells) for row in Table(child, cell).rows)
        return '\n'.join(content).strip()

    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, d).text
            if text.strip():
                lines.append(text)
        elif tag == "tbl":
            for row in Table(child, d).rows:
                cells = [cell_text(c) for c in row.cells]
                if any(cells):
                    lines.append(" | ".join(cells))
    seen = set()
    for section in d.sections:
        for part in (section.header, section.first_page_header, section.even_page_header,
                     section.footer, section.first_page_footer, section.even_page_footer):
            if part.part.partname in seen:
                continue
            seen.add(part.part.partname)
            extra = [p.text for p in part.paragraphs if p.text.strip()]
            extra += [' | '.join(c.text for c in row.cells) for table in part.tables for row in table.rows]
            if extra:
                lines.append('Колонтитул: ' + '\n'.join(extra))
    title = d.core_properties.title or None
    boxes = d.element.xpath('.//w:txbxContent//w:t')
    if boxes:
        lines.append('Текстовые блоки: ' + ' '.join(node.text or '' for node in boxes))
    import xml.etree.ElementTree as ET

    for part in d.part.package.parts:
        if str(part.partname).endswith(('/footnotes.xml', '/endnotes.xml')):
            note = ET.fromstring(part.blob)
            text = ' '.join(t.text for t in note.iter() if t.tag.endswith('}t') and t.text)
            if text:
                lines.append('Сноски: ' + text)
    return ParsedDocument(pages=[Page(None, "\n".join(lines))], title=title)


def parse_xlsx(data: bytes) -> ParsedDocument:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    formulas = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    pages: list[Page] = []
    missing_formulas = []
    truncated = []
    try:
        for ws in wb.worksheets:
            lines = [f"Лист: {ws.title}"]
            if ws.max_row and ws.max_row > 100000 or ws.max_column and ws.max_column > 1000:
                truncated.append(ws.title)
            for row, raw in zip(ws.iter_rows(max_row=min(ws.max_row or 100000, 100000), max_col=min(ws.max_column or 1000, 1000)),
                                formulas[ws.title].iter_rows(max_row=min(ws.max_row or 100000, 100000), max_col=min(ws.max_column or 1000, 1000)), strict=True):
                cells = []
                for cached, original in zip(row, raw, strict=True):
                    value = cached.value
                    if value is None and original.data_type == 'f':
                        missing_formulas.append(f'{ws.title}!{original.coordinate}')
                        value = f'[Формула без сохранённого результата: {original.value}]'
                    cells.append(str(value).strip() if value is not None else '')
                if any(cells):
                    lines.append(" | ".join(cells))
            pages.append(Page(None, "\n".join(lines) if len(lines) > 1 else '',
                              None if len(lines) > 1 else f'Лист «{ws.title}» не содержит данных'))
    finally:
        wb.close()
        formulas.close()
    return ParsedDocument(pages=pages, meta={'uncached_formulas': missing_formulas, 'truncated_sheets': truncated})


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
