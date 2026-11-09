"""Repeatable package checks, including non-attached journals and newly effective RD revisions."""
from __future__ import annotations

import datetime as dt
import hashlib
import uuid

from sqlalchemy import select

from bis.models import Document
from bis.services.document_text import document_text
from bis.services.library_access import SourceContext, document_predicate


def text_hash(text):
    from bis.services.executive_pipeline import norm
    return hashlib.sha256(norm(text).encode()).hexdigest()


def identity(doc):
    card = (doc.data or {}).get('card') or {}
    sheets = card.get('sheets') or [card.get('rd_sheet') or card.get('sheet')]
    return {'kind': doc.kind, 'object_id': str(doc.object_id) if doc.object_id else None,
            'date': doc.date.isoformat() if doc.date else None,
            'rd_code': card.get('rd_code'), 'revision': card.get('revision'),
            'sheets': sorted({str(s).strip() for s in sheets if s is not None})}


def capture_sources(ctx, sources, corpus):
    rows = []
    for doc in sources:
        rows.append({'id': str(doc.id), 'title': doc.title, 'identity': identity(doc),
            'file_key': doc.file_key, 'sha256': file_hash(ctx.storage, doc.file_key),
            'text_sha256': text_hash(corpus.get(doc.id, '')),
            'library_version_id': (doc.data or {}).get('library_version_id')})
    return rows


def file_hash(storage, key):
    try:
        return hashlib.sha256(storage.get(key)).hexdigest() if key else None
    except (OSError, ValueError, TypeError):
        return None


def validate(db, storage, doc):
    from bis.models.libraries import LibraryVersion
    from bis.services.executive_requirements import catalog_signature
    from bis.services.library_access import active_versions
    from bis.services.project_alignment import validate_references

    validate_references(db, doc)

    scope = SourceContext(doc.organization_id, doc.object_id, True)
    matrix = (doc.data or {}).get('requirement_matrix')
    if (doc.data or {}).get('executive_run_id') and matrix is None:
        raise ValueError('Комплект подготовлен без матрицы требований. Повторите проверку и сборку')
    if matrix and matrix.get('catalog_signature') != catalog_signature(db, scope):
        raise ValueError('Нормативное основание изменилось: состав или редакция библиотеки. Повторите проверку требований')
    for item in matrix.get('items', []) if matrix else []:
        source = item['source']
        if source.get('sha256') and source.get('library_version_id'):
            version = db.get(LibraryVersion, uuid.UUID(source['library_version_id']))
            if version is None or file_hash(storage, version.file_key) != source['sha256']:
                raise ValueError('Нормативное основание недоступно или его файл изменился: ' + source['title'])
    for source in (doc.data or {}).get('source_manifest', []):
        current = db.scalar(select(Document).where(Document.id == uuid.UUID(source['id']), document_predicate(scope),
            Document.status.notin_(['cancelled', 'replaced', 'rejected'])))
        if current is None or identity(current) != source['identity'] or current.file_key != source['file_key']:
            raise ValueError('Источник изменился или отозван: ' + source['title'] + '. Повторите проверку')
        if source.get('library_version_id') and db.scalar(select(LibraryVersion.id).where(
                LibraryVersion.id == uuid.UUID(source['library_version_id']), LibraryVersion.id.in_(active_versions(scope)))) is None:
            raise ValueError('Редакция источника заменена: ' + source['title'])
        current_hash = file_hash(storage, current.file_key)
        if current.file_key and current_hash is None:
            raise ValueError('Файл исходного документа недоступен: ' + source['title'])
        if (current_hash != source['sha256']
                or text_hash(document_text(db, current, storage)) != source['text_sha256']):
            raise ValueError('Источник изменился после сборки: ' + source['title'] + '. Повторите проверку')
        meta = source['identity']
        if current.kind == 'project_doc' and meta['rd_code'] and str(meta['revision'] or '').isdigit():
            siblings = db.scalars(select(Document).where(Document.organization_id == doc.organization_id,
                Document.object_id == doc.object_id, Document.kind == 'project_doc',
                Document.status.notin_(['cancelled', 'replaced', 'rejected']), document_predicate(scope))).all()
            end = (doc.data or {}).get('work_end')
            for sibling in siblings:
                other = identity(sibling)
                overlap = not other['sheets'] or not meta['sheets'] or bool(set(other['sheets']) & set(meta['sheets']))
                if (other['rd_code'] == meta['rd_code'] and overlap
                        and str(other['revision'] or '').isdigit() and int(other['revision']) > int(meta['revision'])
                        and (not sibling.date or not end or sibling.date <= dt.date.fromisoformat(end))):
                    raise ValueError('Найдена новая применимая редакция РД: ' + sibling.title + '. Повторите сборку')


def review(ctx, event):
    from bis.services.executive_pipeline import validate_sources

    result = event.result or {}
    doc = ctx.db.get(Document, event.document_id) if event.document_id else None
    errors = [{'field': g['field'], 'message': g['message']} for g in result.get('gaps', [])]
    if doc:
        errors += [{'field': i['field'], 'message': i['message']} for i in doc.issues or [] if i.get('level') == 'error']
        try:
            validate_sources(ctx.db, ctx.storage, doc)
        except (ValueError, OSError) as exc:
            errors.append({'field': 'source_changed', 'message': str(exc)})
    else:
        errors.append({'field': 'draft', 'message': 'Черновик акта ещё не создан'})
    unique = {(e['field'], e['message']): e for e in errors}
    processing = result.get('status') in ('running', 'queued')
    return {'state': 'processing' if processing else 'blocked' if unique else 'ready_for_review',
            'checked_at': dt.datetime.now(dt.UTC).isoformat(), 'blockers': list(unique.values()),
            'requirements': (result.get('plan', {}).get('requirement_matrix') or {}).get('items', []),
            'coverage': (result.get('plan', {}).get('requirement_matrix') or {}).get('coverage'),
            'source_documents': len((doc.data or {}).get('source_manifest', [])) if doc else 0,
            'note': 'Готовность означает возможность проверки инженером; разрешение на последующие работы и подпись не выдаются автоматически.'}
