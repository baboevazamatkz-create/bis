"""Locate editable values in a filled DOCX without sending the document to a model."""
import re
from io import BytesIO
from zipfile import ZipFile

from docx.oxml import parse_xml
from docx.oxml.ns import qn

_ACT = r'(?:аоср|акт(?:\s+(?:аоср|освидетельствования\s+скрытых\s+работ))?)'
_NUMBER = rf'(?:{_ACT}\s+)?№\s*'
_DATE = r'(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|[«"]?\d{1,2}[»"]?\s+[а-яё]+\s+\d{4})'
_HEADER = re.compile(
    rf'^\s*{_NUMBER}(?P<number>[^\n]+?)\s+(?:от\s+)?(?P<date>{_DATE})(?:\s*г\.?)?\s*$', re.IGNORECASE,
)
_NUMBER_ONLY = re.compile(rf'^\s*{_NUMBER}(?P<number>[\w./-]+)\s*$', re.IGNORECASE)
_DATE_ONLY = re.compile(rf'^\s*(?P<date>{_DATE})(?:\s*г\.?)?\s*$', re.IGNORECASE)
_SEPARATOR = re.compile(r'(?:\s*[:：]\s*|\s*\t+\s*|\s+[–—-]\s+)')


def check_source(data):
    # Retained body text is visible in the proof. Word's hidden annotations are not.
    hidden = tuple(qn(f'w:{name}') for name in (
        'ins', 'del', 'moveFrom', 'moveTo', 'txbxContent', 'vanish', 'sdt', 'dataBinding', 'docVars',
    ))
    with ZipFile(BytesIO(data)) as archive:
        for part in archive.namelist():
            if part == 'docProps/custom.xml':
                raise ValueError('Уберите пользовательские свойства Word из копии примера')
            if part.startswith(('word/comments', 'word/embeddings/', 'word/footnotes', 'word/endnotes')):
                raise ValueError('Сохраните копию примера без комментариев, сносок и внедрённых файлов')
            if re.fullmatch(r'word/(?:document|settings|header\d+|footer\d+)\.xml', part):
                root = parse_xml(archive.read(part))
                if next(root.iter(*hidden), None) is not None:
                    raise ValueError('Примите исправления и уберите скрытый текст, переменные и элементы управления Word из примера')


def _label_key(text):
    label = re.sub(r'^\d+[.)]\s+', '', ' '.join(text.split()))
    return label.rstrip(':：').rstrip().casefold()


def adjacent_cell_paragraph(paragraph):
    cell = paragraph._p.getparent()
    if (cell.tag != qn('w:tc') or len(cell.p_lst) != 1
            or len(cell.getparent().tc_lst) != 2):
        return None
    other = cell.getnext()
    if other is None or other.tag != qn('w:tc'):
        return None
    siblings = other.findall(qn('w:p'))
    return siblings[0] if len(siblings) == 1 else None


def _signature_paragraph(paragraph):
    table = paragraph._p.getnext()
    if table is None or table.tag != qn('w:tbl') or len(table.tr_lst) != 1:
        return None
    cells = table.tr_lst[0].tc_lst
    if len(cells) != 2 or any(len(cell.p_lst) != 1 for cell in cells):
        return None
    mark = cells[1].p_lst[0].text
    if re.fullmatch(r'\s*_{2,}\s*\(подпись\)\s*', mark, re.IGNORECASE):
        return cells[0].p_lst[0]
    return None


def find_values(lines, labels):
    positions = {p._p: i for i, p in enumerate(lines)}
    anchors = [p.text for p in lines]
    pattern = re.compile(
        r'^\s*(?:\d+[.)]\s+)?(?P<label>' +
        '|'.join(r'\s+'.join(map(re.escape, label.split())) for label in sorted(labels, key=len, reverse=True)) +
        r')(?=\s|[:：]|$)', re.IGNORECASE,
    )
    values = {}
    occupied = set()

    def add(index, start, end, field=None):
        text = anchors[index]
        value = text[start:end]
        trimmed = value.strip()
        if not trimmed or index in occupied:
            return
        left = start + len(value) - len(value.lstrip())
        right = end - (len(value) - len(value.rstrip()))
        values[f'{index}:{left}:{right}'] = (text, field)

    for index, paragraph in enumerate(lines):
        if index in occupied:
            continue
        anchor = anchors[index]
        header = _HEADER.fullmatch(anchor)
        if header:
            for field in ('number', 'date'):
                add(index, *header.span(field), field)
            occupied.add(index)
            continue
        number = _NUMBER_ONLY.fullmatch(anchor)
        if number:
            other = positions.get(adjacent_cell_paragraph(paragraph))
            date = _DATE_ONLY.fullmatch(anchors[other]) if other is not None else None
            explicit = re.match(rf'^\s*{_ACT}\s+№', anchor, re.IGNORECASE)
            add(index, *number.span('number'), 'number' if date or explicit else None)
            if date:
                add(other, *date.span('date'), 'date')
                occupied.add(other)
            occupied.add(index)
            continue
        copies = re.fullmatch(r'\s*Акт составлен в (\d+) экземпляр(?:е|ах)\.?\s*', anchor, re.IGNORECASE)
        if copies:
            add(index, *copies.span(1), 'copies')
            occupied.add(index)
            continue
        if _label_key(anchor) == 'приложения':
            item = paragraph._p.getnext()
            number = 1
            while (position := positions.get(item)) is not None and re.match(rf'^\s*{number}\.\s+\S', anchors[position]):
                add(position, 0, len(anchors[position]), 'attachment_list' if number == 1 else 'clear_example_value')
                occupied.add(position)
                item = item.getnext()
                number += 1
            continue
        match = pattern.match(anchor)
        if not match:
            continue
        field = labels[_label_key(match.group('label'))]
        separator = _SEPARATOR.match(anchor, match.end())
        if separator and anchor[separator.end():].strip():
            add(index, separator.end(), len(anchor), field)
            occupied.add(index)
            continue
        if not anchor[match.end():].strip(' \t:：'):
            if field.startswith('representative_'):
                signature = positions.get(_signature_paragraph(paragraph))
                if signature is not None and anchors[signature].strip():
                    add(signature, 0, len(anchors[signature]), field.replace('representative_', 'signature_', 1))
                    occupied.update((index, signature))
                    continue
            other = positions.get(adjacent_cell_paragraph(paragraph))
            if other is not None and anchors[other].strip() and not pattern.match(anchors[other]):
                add(other, 0, len(anchors[other]), field)
                occupied.add(other)
                occupied.add(index)
            continue
        # The stock AOSR separates the value into underlined runs even when no colon is printed.
        offset = 0
        for run in paragraph.runs:
            if offset >= match.end() and run.underline and run.text.strip():
                add(index, offset, len(anchor), field)
                occupied.add(index)
                break
            offset += len(run.text)

    for index, anchor in enumerate(anchors):
        if index not in occupied and anchor.strip():
            separator = re.match(r'^[^:\n]{2,120}[:：]\s+', anchor)
            add(index, separator.end() if separator else 0, len(anchor))
    return values
