"""Discover permitted organizational examples and adapt their layout into a fresh editable draft."""
from __future__ import annotations

import hashlib
import io
import re
from copy import deepcopy
from datetime import date

from docx import Document as Word
from docx.oxml.ns import qn
from sqlalchemy import and_, func, or_, select

from bis.models import Document, KnowledgeChunk, KnowledgeSource, Organization
from bis.models.libraries import LibraryConnection, LibraryFile, LibraryVersion
from bis.services.library_access import active_versions, bind_context

TYPES = {'letter': ('письмо', 'письма', 'исх.'), 'order': ('приказ',),
         'memo': ('служебная записка', 'докладная'), 'instruction': ('инструкция',),
         'protocol': ('протокол совещания',)}
REGULATED = {'aosr', 'aook', 'scheme', 'ks2', 'ks3', 'm29', 'writeoff'}
VARIABLE = re.compile(r'исх\.?|вх\.?|уважаем|генеральному|руководителю|директору|получател|подпис|№|\d{2}\.\d{2}\.\d{4}', re.I)
CONTACT = re.compile(r'инн|кпп|огрн|тел\.?|@|www\.|адрес|ул\.|проспект', re.I)


def canonical_type(value):
    value = value.strip().lower()
    for kind, words in TYPES.items():
        if value == kind or any(value.startswith(word) for word in words):
            return kind
    if not value or len(value) > 80:
        raise ValueError('Укажите вид документа')
    return value


def examples(db, org_id, document_type, purpose='', limit=5):
    """Resolve each request against the current scope; no paths, organization names or cached IDs."""
    kind = canonical_type(document_type)
    scope = db.info.get('source_context')
    if scope is None or scope.organization_id != org_id:
        scope = bind_context(db, org_id)
    terms = TYPES.get(kind, (document_type,))
    predicates = [Document.kind == kind, *(Document.title.icontains(term, autoescape=True) for term in terms)]
    lookup_terms = (*terms, 'уважаемый') if kind == 'letter' else terms
    matching = select(KnowledgeSource.document_id).join(KnowledgeChunk, KnowledgeChunk.source_id == KnowledgeSource.id).where(
        KnowledgeSource.organization_id == org_id, or_(*(KnowledgeChunk.tsv.op('@@')(
            func.plainto_tsquery('russian', term)) for term in lookup_terms)))
    predicates.append(and_(Document.kind.in_(['other', 'unknown']), Document.id.in_(matching)))
    docs = db.scalars(select(Document).where(Document.organization_id == org_id, Document.file_key.is_not(None),
        Document.status.notin_(['replaced', 'cancelled', 'rejected']), or_(*predicates),
        or_(Document.data['library_version_id'].astext.is_(None), Document.id.in_(
            select(LibraryVersion.document_id).where(LibraryVersion.id.in_(active_versions(scope))))),
        or_(Document.source != 'generated', Document.status.in_(['approved', 'signed'])),
        or_(Document.object_id.is_(None), Document.object_id == scope.object_id if scope.object_id else False))
        .order_by(Document.created_at.desc()).limit(60)).all()
    found = []
    for doc in docs:
        level = (doc.data or {}).get('library_scope') or ('project' if doc.object_id else 'organization')
        found.append({'id': str(doc.id), 'reference_type': 'document', 'title': doc.title, 'kind': doc.kind,
            'level': level, 'file_name': doc.file_name or '', '_file_key': doc.file_key,
            'reading': (doc.data or {}).get('reading') or {}, 'source': doc.source})
    # Global library forms have no organization Document; still honor active versions and allowed connections.
    versions = db.execute(select(LibraryVersion, LibraryFile, LibraryConnection.level).join(LibraryFile,
        LibraryFile.id == LibraryVersion.file_id).join(LibraryConnection, LibraryConnection.id == LibraryFile.connection_id)
        .where(LibraryVersion.id.in_(active_versions(scope)), LibraryVersion.document_id.is_(None),
               LibraryVersion.form_kind == kind).limit(30)).all()
    for version, item, level in versions:
        found.append({'id': str(version.id), 'reference_type': 'library_version', 'title': item.relative_path,
            'kind': kind, 'level': level, 'file_name': item.relative_path, '_file_key': version.file_key,
            'reading': {}, 'source': 'library'})
    query_words = set(re.findall(r'[а-яёa-z0-9]{4,}', purpose.lower()))
    for item in found:
        title = item['title'].lower()
        item['score'] = (40 if item['kind'] == kind else 15) + {'organization': 30, 'project': 20, 'knowledge': 5}.get(item['level'], 0)
        item['score'] += 10 if item['file_name'].lower().endswith('.docx') else 0
        item['score'] += sum(3 for word in query_words if word in title)
        item['role'] = 'example'  # an example is not a pre-approved empty regulatory form
    found.sort(key=lambda item: (-item['score'], item['id']))
    return found[:max(1, min(limit, 10))]


def public_example(item):
    return {key: value for key, value in item.items() if not key.startswith('_')}


def _plain(element):
    return ''.join(element.xpath('.//w:t/text()'))


def _same_sender(text, org):
    clean = re.sub(r'[^а-яёa-z0-9]', '', text.lower())
    name = re.sub(r'[^а-яёa-z0-9]', '', org.name.lower())
    return bool((org.inn and org.inn in text) or (len(name) > 5 and name in clean))


def _copy_brand(nodes, source_part, target, org):
    """Copy only sender branding, not original body, signatures, comments or external relationships."""
    copied = 0
    for node in nodes:
        text = _plain(node)
        if VARIABLE.search(text):
            continue
        if not (_same_sender(text, org) or CONTACT.search(text) or node.xpath('.//w:drawing')):
            continue
        paragraphs = [node] if node.tag == qn('w:p') else list(node.xpath('.//w:p'))
        known = {value for value in (org.address, org.phone, org.email) if value}
        if any(_plain(p).strip() and not (_same_sender(_plain(p), org) or CONTACT.search(_plain(p)) or _plain(p).strip() in known) for p in paragraphs):
            continue
        if node.xpath('.//w:vanish'):
            continue
        clone = deepcopy(node)
        valid = True
        for element in clone.iter():
            if element.get(qn('r:link')) or element.get(qn('r:id')):
                valid = False  # external hyperlinks/objects are not imported into drafts
            image_id = element.get(qn('r:embed'))
            if image_id:
                part = source_part.related_parts.get(image_id)
                if part is None or not getattr(part, 'content_type', '').startswith('image/'):
                    valid = False
                    break
                new_id, _ = target.part.get_or_add_image(io.BytesIO(part.blob))
                element.set(qn('r:embed'), new_id)
        if valid:
            target._element.append(clone)
            copied += 1
    return copied


def adapt_layout(db, storage, org_id, kind, content, purpose='', sample_id=None):
    """Use a new DOCX package: never carry old body/attachments/hidden comments into the result."""
    choices = examples(db, org_id, kind, purpose, limit=10)
    if sample_id:
        choices = [item for item in choices if item['id'] == str(sample_id)]
        if not choices:
            raise ValueError('Образец не найден среди доступных документов этого вида и организации')
    basis = {'mode': 'fresh_draft', 'sample': None, 'review_required': True,
             'note': 'Подходящий образец не найден. Создан редактируемый черновик без подтверждённой корпоративной формы.'}
    if not choices:
        return content, basis
    item = choices[0]
    try:
        original = storage.get(item['_file_key'])
    except OSError:
        basis['note'] = 'Образец недоступен в хранилище. Создан новый черновик для проверки.'
        return content, basis
    basis.update(sample=public_example(item), sha256=hashlib.sha256(original).hexdigest(), mode='reconstructed',
                 note='Образец найден; его формат нельзя переносить как готовый DOCX. Оформление требует проверки.')
    if not item['file_name'].lower().endswith('.docx'):
        return content, basis
    try:
        sample, result = Word(io.BytesIO(original)), Word(io.BytesIO(content))
        org = db.get(Organization, org_id)
        if org is None:
            raise LookupError('Организация не найдена')
        source_section, target_section = sample.sections[0], result.sections[0]
        for attr in ('left_margin', 'right_margin', 'top_margin', 'bottom_margin', 'page_width', 'page_height'):
            setattr(target_section, attr, getattr(source_section, attr))
        normal = sample.styles['Normal']
        if normal.font.name:
            result.styles['Normal'].font.name = normal.font.name
            for paragraph in result.paragraphs:
                for run in paragraph.runs:
                    run.font.name = normal.font.name
        # Only the leading sender area may supply branding. Stop before a recipient/date/body block.
        prefix = []
        for node in list(sample.element.body)[:10]:
            text = _plain(node)
            if VARIABLE.search(text) or (text.strip() and not (_same_sender(text, org) or CONTACT.search(text))):
                break
            prefix.append(node)
        header_nodes = list(source_section.header._element)
        sender_confirmed = any(_same_sender(_plain(node), org) for node in [*header_nodes, *prefix])
        copied = 0
        if sender_confirmed:
            copied += _copy_brand(header_nodes, source_section.header.part, target_section.header, org)
            copied += _copy_brand(prefix, sample.part, target_section.header, org)
        if copied:
            for node in list(result.element.body)[:4]:
                text = _plain(node)
                if node.tag != qn('w:p') or (text.strip() and not (_same_sender(text, org) or CONTACT.search(text))):
                    break
                result.element.body.remove(node)
        basis.update(mode='sender_brand_and_styles' if copied else 'styles_only',
            sender_confirmed=sender_confirmed,
            note='Оформление адаптировано из архивного образца. Старый текст, адресат, даты, номера и подписи не переносились; проверьте черновик.')
        output = io.BytesIO()
        result.save(output)
        return output.getvalue(), basis
    except Exception as error:  # noqa: BLE001 — a malformed example must not prevent a fresh draft
        basis['note'] = 'Образец не удалось адаптировать: ' + type(error).__name__ + '. Создан новый черновик.'
        return content, basis


def render_administrative(org, document_type, subject, paragraphs, signer_position, signer_name, fields):
    from bis.documents.common import new_document, para, to_bytes

    doc = new_document()
    para(doc, org.name, bold=True)
    para(doc, document_type.upper(), bold=True)
    para(doc, f'{date.today():%d.%m.%Y} № [присвоить номер]')
    para(doc, subject, bold=True)
    for label, value in fields.items():
        para(doc, f'{label}: {value}')
    for text in paragraphs:
        para(doc, text)
    para(doc, signer_position or '[Должность подписанта: уточнить]')
    para(doc, signer_name or '[Подписант: уточнить]')
    return to_bytes(doc)
