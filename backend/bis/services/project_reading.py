"""Incremental native/layout/OCR project reader; visual observations never masquerade as text proof."""
from __future__ import annotations

import base64
import hashlib
import io
import re
import uuid
from pathlib import Path

from sqlalchemy import select

from bis.models import Document, KnowledgeChunk, KnowledgeSource
from bis.models.project_reading import ProjectReading
from bis.services import project_structure as structure
from bis.services.library_access import SourceContext, document_predicate
from bis.services.project_facts import SourceInProgress, digest, scope


def owned(ctx, document_id):
    doc = ctx.db.get(Document, uuid.UUID(str(document_id)))
    if doc is None or doc.organization_id != ctx.org_id or doc.kind != 'project_doc' or not doc.object_id:
        raise LookupError('Исходный проектный документ не найден')
    scope(ctx, doc.object_id)
    if not ctx.db.scalar(select(Document.id).where(Document.id == doc.id,
            document_predicate(SourceContext(ctx.org_id, doc.object_id, True)),
            Document.status.notin_(['cancelled', 'replaced', 'rejected']))):
        raise LookupError('Редакция проекта недоступна')
    return doc


def inputs(ctx, doc):
    rows = ctx.db.execute(select(KnowledgeChunk, KnowledgeSource).join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
        .where(KnowledgeSource.document_id == doc.id, KnowledgeSource.organization_id == ctx.org_id)
        .order_by(KnowledgeSource.id, KnowledgeChunk.ord)).all()
    raw = ctx.storage.get(doc.file_key) if doc.file_key else b''
    models = [(name, e.provider.model, e.capabilities) for name, e in ctx.gateway.entries.items()] if ctx.gateway else []
    routes = ctx.gateway.routes if ctx.gateway else {}
    signature = digest([structure.VERSION, hashlib.sha256(raw).hexdigest(), str(doc.date), str(doc.object_id), doc.file_name, doc.mime,
        [(str(s.id), c.page, c.content, bool((s.meta or {}).get('vision'))) for c, s in rows], models, routes])
    return raw, rows, signature


def page_source(raw, doc):
    """Physical PDF pages and workbook sheets are separate; DOCX/TXT have no invented page numbers."""
    ext = Path(doc.file_name or '').suffix.lower()
    if ext == '.pdf' or doc.mime == 'application/pdf':
        from pypdf import PdfReader
        pdf = PdfReader(io.BytesIO(raw))
        return len(pdf.pages), lambda i: {'physical_page': i + 1, 'label': f'PDF · страница {i + 1}',
            'text': pdf.pages[i].extract_text(extraction_mode='layout') or '', 'origin': 'pdf_layout'}
    if ext in ('.xlsx', '.xlsm'):
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        units = [(name, start, min(start + 199, workbook[name].max_row)) for name in workbook.sheetnames
                 for start in range(1, max(1, workbook[name].max_row) + 1, 200)]
        def get(i):
            name, start, end = units[i]
            lines = []
            for values in workbook[name].iter_rows(min_row=start, max_row=end, values_only=True):
                if any(v is not None for v in values):
                    lines.append(' | '.join('' if v is None else str(v) for v in values))
            header = None
            if start > 1:
                for values in workbook[name].iter_rows(min_row=1, max_row=20, values_only=True):
                    candidate = ' | '.join('' if v is None else str(v) for v in values)
                    found = {field for value in structure.cells(candidate) for field, pattern in structure.HEADER.items() if re.search(pattern, value, re.I)}
                    if {'name', 'unit', 'quantity'} <= found:
                        header = candidate
            label = name if workbook[name].max_row <= 200 else f'{name} · строки {start}–{end}'
            return {'physical_page': None, 'label': label, 'text': '\n'.join(lines), 'origin': 'xlsx_table', 'header_context': header}
        return len(units), get
    if ext in ('.png', '.jpg', '.jpeg', '.tif', '.tiff'):
        return 1, lambda i: {'physical_page': None, 'label': doc.file_name, 'text': '', 'origin': 'image'}
    from bis.rag.parsers import parse_file
    parsed = parse_file(raw, doc.file_name or 'project.txt', doc.mime)
    return len(parsed.pages), lambda i: {'physical_page': None, 'label': 'Документ', 'text': parsed.pages[i].text, 'origin': 'native_text'}


def images_for_page(raw, doc, index):
    """Full sheet plus title-block crop; image coordinates are observations, never a measured length."""
    from PIL import Image
    if Path(doc.file_name or '').suffix.lower() == '.pdf' or doc.mime == 'application/pdf':
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(raw)
        page = pdf[index]
        width, height = page.get_size()
        picture = page.render(scale=min(3, 5200 / max(width, height))).to_pil().convert('RGB')
        page.close()
        pdf.close()
    else:
        picture = Image.open(io.BytesIO(raw)).convert('RGB')
        picture.thumbnail((2600, 2600))
    w, h = picture.size
    preview = picture.copy()
    preview.thumbnail((1800, 1800))
    parts = [preview, picture.crop((int(w * .48), int(h * .62), w, h))]
    parts += [picture.crop(box) for box in ((0, 0, w // 2, h // 2), (w // 2, 0, w, h // 2),
                                           (0, h // 2, w // 2, h), (w // 2, h // 2, w, h))]
    out = []
    for part in parts:
        part.thumbnail((2200, 2200))
        buf = io.BytesIO()
        part.save(buf, 'JPEG', quality=90)
        out.append({'mime': 'image/jpeg', 'data': base64.b64encode(buf.getvalue()).decode()})
    return out


def read(ctx, document_id, *, budget=8, with_vision=False, force=False):
    doc = owned(ctx, document_id)
    if ctx.db.scalar(select(Document.id).where(Document.id == doc.id).with_for_update(skip_locked=True)) is None:
        raise SourceInProgress('Проект уже разбирается. Повторите запрос после завершения текущей порции')
    try:
        raw, chunks, signature = inputs(ctx, doc)
        count, get_page = page_source(raw, doc)
    except (OSError, ValueError) as exc:
        raise ValueError('Не удалось прочитать файл проекта: ' + str(exc)) from exc
    visual_signature = digest([signature, True])
    signature = digest([signature, with_vision])
    base_signature = signature
    if force:
        signature = digest([signature, str(uuid.uuid4())])
    previous = ctx.db.get(ProjectReading, uuid.UUID(doc.data['project_reading_id'])) if (doc.data or {}).get('project_reading_id') else None
    reusable = {base_signature, visual_signature} if not with_vision else {base_signature}
    if not force and previous and previous.payload.get('base_signature') in reusable:
        reading = previous
    else:
        reading = ctx.db.scalar(select(ProjectReading).where(ProjectReading.document_id == doc.id, ProjectReading.input_signature == signature))
    if reading is None:
        reading = ProjectReading(organization_id=ctx.org_id, object_id=doc.object_id, document_id=doc.id,
            input_signature=signature, file_key=doc.file_key, file_name=doc.file_name,
            file_sha256=hashlib.sha256(raw).hexdigest(), result_sha256=digest({}), state='partial',
            payload={'schema': structure.VERSION, 'base_signature': base_signature, 'document_date': doc.date.isoformat() if doc.date else None, 'pages': [], 'coverage': {}})
        ctx.db.add(reading)
        ctx.db.flush()
    done = {p['ordinal']: p for p in reading.payload['pages']}
    pending = [i for i in range(count) if i not in done]
    for index in pending[:max(1, min(budget, 40))]:
        source = get_page(index)
        text = source['text']
        # Only indexed OCR/native text on the actual physical page can supplement a sparse PDF page.
        page_chunks = [c.content for c, s in chunks if not (s.meta or {}).get('vision')
                       and ((source['physical_page'] is not None and c.page == source['physical_page'])
                            or (count == 1 and c.page is None))]
        if len(text.strip()) < 30 and page_chunks:
            text = '\n'.join(dict.fromkeys(page_chunks))
            source['origin'] = 'indexed_text'
        if not text.strip():
            from bis import ocr
            if ocr.available() and (source['physical_page'] is not None or source['origin'] == 'image'):
                try:
                    picture = images_for_page(raw, doc, index)[0]
                    text = ocr.recognize(base64.b64decode(picture['data']), picture['mime'])
                    source['origin'] = 'ocr'
                except Exception:  # noqa: BLE001 — keep the page explicitly unread when OCR is unavailable
                    source['origin'] = 'ocr_unavailable'
        images = None
        if with_vision:
            from bis.brain.vision import available
            if available(ctx.gateway) and (source['physical_page'] is not None or source['origin'] == 'image'):
                images = images_for_page(raw, doc, index)
        if not text.strip() and not images:
            result = {'claims': [], 'items': [], 'observations': [], 'rejected': 0, 'method': 'unread',
                'complete': False, 'warnings': ['Нет читаемого текста страницы; нужны OCR или модель зрения']}
        else:
            previous_header, header_page = source.pop('header_context', None), None
            prior = done.get(index - 1)
            if (source['physical_page'] is not None and prior and prior['physical_page'] is not None
                    and re.search(r'продолжение\s+(?:таблиц|спецификац|ведомост)', text, re.I)):
                previous_rows = [i for i in prior['items'] if i.get('header_quote')]
                if previous_rows:
                    previous_header = previous_rows[-1]['header_quote']
                    header_page = previous_rows[-1].get('header_page') or prior['physical_page']
            result = structure.read_page(ctx.gateway, text, images=images, previous_header=previous_header)
            for item in result['items']:
                if item.get('header_quote'):
                    item['header_page'] = source['physical_page'] if item['header_quote'] in text else header_page
        done[index] = {**source, **result, 'ordinal': index, 'text_sha256': hashlib.sha256(text.encode()).hexdigest()}
        done[index].pop('text', None)
    pages = [done[i] for i in sorted(done)]
    coverage = {'total': count, 'processed': len(pages), 'remaining': count - len(pages),
        'unread': [p['label'] for p in pages if p['method'] == 'unread'],
        'review': [p['label'] for p in pages if not p['complete'] and p['method'] != 'unread'],
        'scope': 'Извлечены подтверждённые надписи и таблицы; геометрия и полнота инженерного понимания требуют проверки.'}
    reading.payload = {**reading.payload, 'pages': pages, 'coverage': coverage}
    reading.result_sha256 = digest(reading.payload)
    reading.state = 'ready' if count > 0 and not coverage['remaining'] and not coverage['unread'] and not coverage['review'] else 'partial'
    doc.data = {**(doc.data or {}), 'project_reading_id': str(reading.id)}
    ctx.db.flush()
    return reading


def view(reading):
    return {'id': str(reading.id), 'document_id': str(reading.document_id), 'object_id': str(reading.object_id),
            'state': reading.state, 'result_sha256': reading.result_sha256, **reading.payload}


def current(ctx, document_id):
    doc = owned(ctx, document_id)
    marker = (doc.data or {}).get('project_reading_id')
    row = ctx.db.get(ProjectReading, uuid.UUID(marker)) if marker else None
    if row is None:
        return None
    _, _, signature = inputs(ctx, doc)
    return row if row.payload.get('base_signature') in (digest([signature, False]), digest([signature, True])) else None
