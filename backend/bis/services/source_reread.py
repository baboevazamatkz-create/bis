"""Re-read stored original uploads without requiring another upload or a cloud classifier."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, select

from bis.models import Document, KnowledgeSource
from bis.services.library_sync import connection_lease


def process(document_id):
    from bis.brain.build import apply_card
    from bis.brain.card import rules_card
    from bis.db import SessionLocal
    from bis.rag.embeddings import get_query_embedder
    from bis.rag.ingest import ingest_pages
    from bis.services import archive
    from bis.services.storage import get_storage

    with connection_lease(document_id) as acquired:
        if not acquired:
            return
        with SessionLocal() as db:
            doc = db.get(Document, document_id)
            if doc is None or not doc.file_key or (doc.data or {}).get('read_state') not in ('queued', 'running'):
                return
            request = doc.data.get('read_request')
            key = doc.file_key
            doc.data = {**doc.data, 'read_state': 'running', 'read_started_at': dt.datetime.now(dt.UTC).isoformat()}
            db.commit()
            try:
                pages, method = archive._pages(get_storage().get(key), doc.file_name or 'file', doc.mime)
                text = '\n'.join(p.text for p in pages)
                if not text.strip():
                    raise ValueError(next((p.warning for p in pages if p.warning), 'Из файла не извлечён текст'))
                db.refresh(doc)
                if doc.file_key != key or doc.data.get('read_request') != request:
                    return
                old_ids = list(db.scalars(select(KnowledgeSource.id).where(KnowledgeSource.document_id == doc.id)))
                dtype, _ = archive.detect_type(None, text, doc.file_name or doc.title)
                if doc.kind == 'other' and dtype != 'unknown':
                    doc.kind = archive.KIND.get(dtype, doc.kind)
                embedder = get_query_embedder()
                src = ingest_pages(db, embedder, org_id=doc.organization_id, pages=pages,
                    kind=archive.KNOWLEDGE.get(dtype, 'org_document'), title=doc.title, document_id=doc.id)
                src.file_key, src.file_name, src.mime = key, doc.file_name, doc.mime
                reading = archive.reading_report(pages, method)
                src.meta = {**(src.meta or {}), 'reading': reading}
                if old_ids:
                    db.execute(delete(KnowledgeSource).where(KnowledgeSource.id.in_(old_ids)))
                apply_card(db, embedder, doc, rules_card(text, doc.kind, doc.title), reembed=False)
                doc.data = {**doc.data, 'read_state': 'ready', 'reading_error': None, 'reading': reading,
                            'text_from': method, 'read_finished_at': dt.datetime.now(dt.UTC).isoformat()}
                if doc.kind == 'contract':
                    doc.data = {**doc.data, 'contract_kind': archive.contract_kind(text)}
                db.commit()
            except Exception as exc:  # noqa: BLE001 — keep the old index and original on failure
                db.rollback()
                doc = db.get(Document, document_id)
                if doc and doc.data.get('read_request') == request:
                    doc.data = {**doc.data, 'read_state': 'error', 'reading_error': str(exc)[:500]}
                    db.commit()


def process_many(ids):
    for document_id in ids:
        process(document_id)


def queue_documents(db, documents):
    ids = []
    for doc in documents:
        doc.data = {**(doc.data or {}), 'read_state': 'queued', 'read_request': str(uuid.uuid4())}
        ids.append(doc.id)
    db.commit()
    return ids


def register_job():
    from bis.jobs import job

    @job('reread', 'Перечитать исходные документы', every_minutes=1)
    def recover(db, org, today):
        cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)).isoformat()
        ids = list(db.scalars(select(Document.id).where(Document.organization_id == org.id,
            (Document.data['read_state'].astext == 'queued') |
            ((Document.data['read_state'].astext == 'running') & (Document.data['read_started_at'].astext < cutoff)))
            .limit(10)))
        db.commit()
        process_many(ids)
        return {'processed': len(ids)}
