"""Reviewed mappings fill originals; no substitute built-in form in strict mode."""
from __future__ import annotations

import io
import re

from pydantic import BaseModel, Field
from sqlalchemy import select

from bis.documents.templates import template_context
from bis.llm.extract import extract
from bis.models.libraries import LibraryConnection, LibraryFile, LibraryVersion
from bis.services.library_access import active_versions, bind_context

FIELDS = {
    'number': 'Номер', 'date': 'Дата составления', 'object_name': 'Объект строительства',
    'works': 'Выполненные работы', 'project_docs': 'Рабочая документация', 'materials': 'Материалы',
    'executive_docs': 'Исполнительные документы', 'work_start': 'Начало работ', 'work_end': 'Окончание работ',
    'norms': 'Нормативные документы', 'next_works': 'Последующие работы', 'attachments': 'Приложения',
    'developer_text': 'Застройщик', 'builder_text': 'Лицо, осуществляющее строительство', 'designer_text': 'Проектировщик',
    'subject': 'Тема письма', 'body': 'Текст письма', 'addressee': 'Адресат', 'sender_text': 'Отправитель', 'signer_name': 'ФИО подписанта', 'signer_position': 'Должность подписанта', 'aosr_refs': 'АОСР конструкций', 'representatives': 'Представители участников', 'other_representatives': 'Иные представители',
    'tests': 'Испытания конструкций', 'permit': 'Разрешение на нагружение', 'conditions': 'Условия нагружения', 'load_percent': 'Процент нагрузки',
    'additional_info': 'Дополнительные сведения', 'performer_text': 'Исполнитель работ', 'greeting': 'Обращение', 'executor': 'Исполнитель письма', 'in_reply_to': 'В ответ на письмо',
}
REQUIRED = {
    'aosr': {'number', 'date', 'works', 'object_name', 'work_start', 'work_end', 'project_docs', 'materials', 'executive_docs', 'norms', 'developer_text', 'builder_text', 'representatives'},
    'aook': {'number', 'date', 'works', 'object_name', 'work_start', 'work_end', 'project_docs', 'materials', 'executive_docs', 'norms', 'developer_text', 'builder_text', 'representatives', 'aosr_refs', 'tests', 'permit'},
    'letter': {'body'}, 'work_journal': {'rows'}, 'id_registry': {'rows'},
}
FIELDS['rows'] = 'Строки журнала / реестра'


class Entry(BaseModel):
    field: str
    target: str
    anchor: str = ''
    mode: str = 'append'


class Mapping(BaseModel):
    format: str
    entries: list[Entry] = Field(default_factory=list, max_length=200)


class Proposal(BaseModel):
    entries: list[Entry] = Field(default_factory=list, max_length=200)


def paragraphs(doc):
    result = list(doc.paragraphs)
    seen = set()
    def visit(tables):
        for table in tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell._tc in seen:
                        continue
                    seen.add(cell._tc)
                    result.extend(cell.paragraphs)
                    visit(cell.tables)
    visit(doc.tables)
    for section in doc.sections:
        result.extend(section.header.paragraphs)
        result.extend(section.footer.paragraphs)
    return result


def targets(data, filename):
    low = filename.lower()
    if low.endswith('.docx'):
        from docx import Document
        return 'docx', {str(i): p.text for i, p in enumerate(paragraphs(Document(io.BytesIO(data))))}
    if low.endswith('.xlsx'):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data))
        return 'xlsx', {f'{ws.title}!{c.coordinate}': str(c.value or '') for ws in wb for row in ws for c in row
                        if c.value is not None and c.data_type != 'f'}
    if low.endswith('.pdf'):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        fields = reader.get_fields() or {}
        return 'pdf', {str(k): str(v.get('/TU') or k) for k, v in fields.items()}
    raise ValueError('Автозаполнение поддерживает DOCX, XLSX и PDF')


def prepare(data, filename, gateway):
    fmt, places = targets(data, filename)
    entries = []
    for target, label in places.items():
        low = label.lower()
        hits = [field for field, title in FIELDS.items() if title.lower() in low or '{{ ' + field + ' }}' in label]
        if len(hits) == 1:
            entries.append(Entry(field=hits[0], target=target, anchor=label, mode='append'))
    proposed = extract(gateway, Proposal, '\n'.join(f'{k}: {v}' for k, v in places.items()), task='executive_document',
        instructions='Предложи карту полей строительного бланка. Не меняй текст. field только из: ' +
        ', '.join(FIELDS) + '. target — дословный адрес из входных данных, anchor — полный дословный текст этого адреса. '
        'mode=append. Сомнительные поля пропускай.', fallback=lambda _: Proposal(entries=entries)).data
    safe = [e for e in (proposed.entries if proposed else entries)
            if e.field in FIELDS and e.target in places and e.anchor == places[e.target]]
    by_target = {e.target: e for e in entries}
    by_target.update({e.target: e for e in safe})
    return Mapping(format=fmt, entries=list(by_target.values()))


def validate_mapping(data, filename, mapping, kind):
    if kind not in REQUIRED:
        raise ValueError('Вид формы пока не поддерживает автозаполнение')
    fmt, places = targets(data, filename)
    if mapping.format not in (fmt, 'pdf_coordinates' if fmt == 'pdf' else fmt):
        raise ValueError('Формат карты не соответствует исходному файлу')
    used = set()
    for e in mapping.entries:
        if e.field not in FIELDS or e.mode not in ('append', 'replace') or e.target in used:
            raise ValueError('Неизвестное поле, повтор адреса или недопустимый способ заполнения')
        used.add(e.target)
        if mapping.format == 'pdf_coordinates':
            parts = e.target.split(':')
            if len(parts) != 5:
                raise ValueError('Адрес PDF: номер страницы:x:y:ширина:высота (в пунктах от верхнего края)')
            try:
                page, x, y, width, height = map(float, parts)
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(data))
                if not page.is_integer() or not 1 <= page <= len(reader.pages):
                    raise ValueError('Страница вне PDF')
                box = reader.pages[int(page) - 1].mediabox
                if min(x, y) < 0 or min(width, height) <= 0 or x + width > float(box.width) or y + height > float(box.height):
                    raise ValueError('Область заполнения вне страницы')
            except (ValueError, TypeError) as exc:
                raise ValueError('Некорректная область заполнения PDF') from exc
        elif fmt == 'xlsx' and e.target not in places and e.anchor == '':
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data))
            try:
                sheet, cell = e.target.rsplit('!', 1)
                if wb[sheet][cell].value is not None:
                    raise ValueError('Ячейка не пустая')
            except (KeyError, ValueError, AttributeError) as exc:
                raise ValueError('Некорректная или непустая ячейка Excel') from exc
        elif e.target not in places or places[e.target] != e.anchor:
            raise ValueError('Поле не найдено или исходный бланк изменился: ' + e.target)
    missing = REQUIRED[kind] - {e.field for e in mapping.entries}
    if missing:
        raise ValueError('Не сопоставлены обязательные поля: ' + ', '.join(FIELDS[k] for k in sorted(missing)))


def value_text(value):
    if value is None:
        return ''
    if isinstance(value, list):
        return '\n'.join(value_text(v) for v in value)
    if isinstance(value, dict):
        return '; '.join(value_text(v) for v in value.values() if v is not None)
    if hasattr(value, 'strftime'):
        return value.strftime('%d.%m.%Y')
    return str(value)


def filled(data, filename, mapping, values):
    def text(entry):
        value = value_text(values.get(entry.field))
        token = re.compile(r'{{\s*' + re.escape(entry.field) + r'\s*}}')
        if token.search(entry.anchor):
            return token.sub(lambda _: value, entry.anchor)
        if entry.mode == 'replace' or not entry.anchor.strip('_ .\t'):
            return value
        return entry.anchor.rstrip() + '\n' + value
    out = io.BytesIO()
    if mapping.format == 'docx':
        from docx import Document
        doc = Document(io.BytesIO(data))
        lines = paragraphs(doc)
        for e in mapping.entries:
            p = lines[int(e.target)]
            # Keep paragraph/cell properties and the first run style rather than replacing the paragraph itself.
            if p.runs:
                p.runs[0].text = text(e)
                for run in p.runs[1:]:
                    run.text = ''
            else:
                p.add_run(text(e))
        if any('{{' in p.text or '{%' in p.text for p in paragraphs(doc)):
            raise ValueError('В бланке остались несопоставленные заполнители')
        doc.save(out)
    elif mapping.format == 'xlsx':
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data))
        for e in mapping.entries:
            sheet, cell = e.target.rsplit('!', 1)
            if e.field == 'rows' and isinstance(values.get('rows'), list):
                from copy import copy

                start = wb[sheet][cell]
                for offset, row_values in enumerate(values['rows']):
                    for col, v in enumerate(row_values if isinstance(row_values, list) else [row_values]):
                        target = wb[sheet].cell(start.row + offset, start.column + col)
                        if target.data_type == 'f':
                            raise ValueError('Строки данных пересекают формулу Excel')
                        prototype = wb[sheet].cell(start.row, start.column + col)
                        if offset:
                            target._style = copy(prototype._style)
                        value = value_text(v)
                        target.value = "'" + value if value.startswith(('=', '+', '-', '@')) else value
                continue
            value = text(e)
            # User text must never become an Excel formula.
            wb[sheet][cell] = "'" + value if value.startswith(('=', '+', '-', '@')) else value
        wb.save(out)
    elif mapping.format == 'pdf':
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
        for page in writer.pages:
            writer.update_page_form_field_values(page, {e.target: value_text(values.get(e.field)) for e in mapping.entries}, auto_regenerate=False)
        writer.write(out)
    elif mapping.format == 'pdf_coordinates':
        from pypdf import PdfReader, PdfWriter
        from reportlab.lib.utils import simpleSplit
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas

        from bis.config import get_settings

        font = get_settings().library_pdf_font
        if not font.is_file():
            import os
            from pathlib import Path

            font = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'arial.ttf'
        if not font.is_file():
            raise ValueError('Не найден шрифт PDF с кириллицей. Укажите BIS_LIBRARY_PDF_FONT')
        pdfmetrics.registerFont(TTFont('BISLibrary', str(font)))
        reader = PdfReader(io.BytesIO(data))
        writer = PdfWriter(clone_from=reader)
        for n, page in enumerate(writer.pages, 1):
            overlay = io.BytesIO()
            width, height = float(page.mediabox.width), float(page.mediabox.height)
            c = canvas.Canvas(overlay, pagesize=(width, height))
            c.setFont('BISLibrary', 9)
            for e in mapping.entries:
                page_no, x, y, w, h = map(float, e.target.split(':'))
                if page_no != n:
                    continue
                lines = simpleSplit(value_text(values.get(e.field)), 'BISLibrary', 9, w)
                if len(lines) * 11 > h:
                    raise ValueError('Текст не помещается в подтверждённую область PDF: ' + FIELDS[e.field])
                for j, line in enumerate(lines):
                    c.drawString(x, height - y - 9 - j * 11, line)
            c.save()
            page.merge_page(PdfReader(overlay).pages[0])
        writer.write(out)
    else:
        raise ValueError('Карта заполнения ещё не подготовлена')
    return out.getvalue()


def select_form(db, org_id, kind, object_id=None):
    scope = bind_context(db, org_id, object_id) if object_id else db.info.get('source_context') or bind_context(db, org_id)
    choices = db.execute(select(LibraryVersion, LibraryConnection.level).join(LibraryFile,
        LibraryFile.id == LibraryVersion.file_id).join(LibraryConnection, LibraryConnection.id == LibraryFile.connection_id)
        .where(LibraryVersion.id.in_(active_versions(scope)), LibraryVersion.form_kind == kind,
               LibraryVersion.approved_at.is_not(None))).all()
    if not choices:
        raise ValueError('Нет подтверждённой формы ' + kind.upper() + ' в подключённых библиотеках')
    rank = {'project': 0, 'organization': 1, 'knowledge': 2}
    best = min(rank[level] for _, level in choices)
    found = [v for v, level in choices if rank[level] == best]
    if len(found) > 1:
        selected = db.info.get('library_form_id')
        found = [v for v in found if str(v.id) == str(selected)] if selected else found
    if len(found) != 1:
        raise ValueError('Найдено несколько подтверждённых форм; выберите форму в задании')
    return found[0]


def render(db, storage, org_id, kind, data, object_id=None):
    version = select_form(db, org_id, kind, object_id)
    item = db.get(LibraryFile, version.file_id)
    mapping = Mapping.model_validate(version.mapping)
    original = storage.get(version.file_key)
    import hashlib

    if hashlib.sha256(original).hexdigest() != version.sha256:
        raise ValueError('Сохранённый оригинал формы изменился')
    validate_mapping(original, item.relative_path, mapping, kind)
    values = template_context(data) if isinstance(data, BaseModel) else data
    if isinstance(data, BaseModel) and hasattr(data, "representatives"):
        from bis.documents.aosr import REPRESENTATIVE_ROLES

        values["representatives"] = "\n".join(
            REPRESENTATIVE_ROLES.get(role, {}).get("title", role) + ": " + person.format()
            for role, person in data.representatives.items())
        values["other_representatives"] = "\n".join(person.format() for person in data.other_representatives)
    if isinstance(data, BaseModel) and hasattr(data, "permit"):
        from bis.documents.aook import PERMITS

        values["permit"] = PERMITS[data.permit].format(load=data.load_percent or "", conditions=data.conditions or "")
    content = filled(original, item.relative_path, mapping, values)
    db.info['used_library_form'] = {'version_id': str(version.id), 'sha256': version.sha256,
        'file_name': item.relative_path.rsplit('/', 1)[-1], 'mime': version.mime}
    return content
