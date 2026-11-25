"""Read-only outline and structural checks for DOCX authoring."""
import hashlib
import io
import json
import re
from zipfile import ZipFile

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import qn

from bis.services.library_forms import paragraphs

_BODY_PART = re.compile(r'word/(?:document|header\d+|footer\d+)\.xml')
_BLOCK = {qn('w:p'), qn('w:tbl')}
_GEOMETRY = {qn('w:' + name) for name in ('p', 'tbl', 'tr', 'tc', 'tblGrid', 'pPr', 'tblPr', 'trPr', 'tcPr')}


def outline(data, *, precise=True):
    doc = Document(io.BytesIO(data))
    lines = {p._p: (str(index), p.text) for index, p in enumerate(paragraphs(doc, existing_parts_only=precise))}

    def nodes(parent):
        result = []
        for node in parent:
            if node.tag == qn('w:p') and node in lines:
                index, text = lines[node]
                result.append({'type': 'paragraph', 'id': index, 'text': text})
            elif node.tag == qn('w:tbl'):
                result.append({'type': 'table', 'rows': [[nodes(cell) for cell in row.tc_lst] for row in node.tr_lst]})
        return result

    body = nodes(doc.element.body)
    seen = {doc.element}
    for node in lines:
        root = node.getroottree().getroot()
        if root not in seen:
            body.extend(nodes(root))
            seen.add(root)
    return body


def geometry(data, *, ignore_editor_ids=False):
    """Record paragraph/table order and properties, excluding content runs."""
    def attributes(node):
        return sorted((key, value) for key, value in node.attrib.items()
                      if not ignore_editor_ids or not key.split('}')[-1].startswith(('rsid', 'paraId', 'textId')))

    structure = []
    with ZipFile(io.BytesIO(data)) as z:
        for name in sorted(name for name in z.namelist() if _BODY_PART.fullmatch(name)):
            tree = parse_xml(z.read(name))
            shape = []
            for node in tree.iter(*_GEOMETRY):
                props = node.tag not in _BLOCK | {qn('w:tr'), qn('w:tc')}
                children = [(child.tag, attributes(child)) for child in node.iter()] if props else []
                shape.append((node.tag, attributes(node), children))
            structure.append((name, shape))
    return structure


def fingerprint(data, *, example=False):
    from bis.services.library_forms import normalized_blank_anchor

    layout = outline(data)
    if not example:
        def normalize(nodes):
            for node in nodes:
                if node['type'] == 'paragraph':
                    node['text'] = normalized_blank_anchor(node['text'])
                else:
                    for row in node['rows']:
                        for cell in row:
                            normalize(cell)
        normalize(layout)
    relevant = [layout, geometry(data, ignore_editor_ids=True)]
    return hashlib.sha256(json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def verify_geometry(original, filled):
    if geometry(original) != geometry(filled):
        raise ValueError('При заполнении изменилась структура DOCX. Проверьте карту полей.')
