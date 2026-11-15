"""Shared access to the complete indexed text, including OCR and visual descriptions."""

from __future__ import annotations

import contextlib

from sqlalchemy import select

from bis.models import KnowledgeChunk, KnowledgeSource
from bis.rag.search import _hit


def document_chunks(db, doc):
    rows = db.execute(select(KnowledgeChunk, KnowledgeSource).join(
        KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id).where(
        KnowledgeSource.organization_id == doc.organization_id, KnowledgeSource.document_id == doc.id,
        KnowledgeSource.status.notin_(['cancelled', 'replaced']))
        .order_by(KnowledgeSource.created_at, KnowledgeSource.id, KnowledgeChunk.ord)).all()
    # Text and vision are separate sources: never interleave their chunk ordinals.
    rows.sort(key=lambda row: bool((row[1].meta or {}).get("vision")))
    return [_hit(chunk, source) for chunk, source in rows]


def document_text(db, doc, storage=None):
    chunks = document_chunks(db, doc)
    text = "\n".join(c.content for c in chunks)
    if not text.strip() and storage is not None and doc.file_key:
        from bis.rag.parsers import UnsupportedFormat, parse_file

        with contextlib.suppress(UnsupportedFormat):
            text = parse_file(storage.get(doc.file_key), doc.file_name or "file", doc.mime).text
    return text


def query_windows(chunks, query, max_chars=1500):
    """Scan the entire available index, weighting rare terms over document-wide boilerplate."""
    import math
    import re
    from collections import Counter

    stop = {'какой', 'какая', 'какие', 'какое', 'сколько', 'найди', 'покажи', 'скажи', 'этого', 'этому', 'данному', 'пожалуйста'}
    terms = list(dict.fromkeys(word[:5] for word in re.findall(r'[а-яёa-z0-9]{3,}', query.lower()) if word not in stop))
    patterns = {term: re.compile(r'\b' + re.escape(term), re.I) for term in terms}
    matches = [(hit, {term: pattern.search(hit.content) for term, pattern in patterns.items()}) for hit in chunks]
    frequency = Counter(term for _, found in matches for term, match in found.items() if match)
    weights = {term: 1 + math.log(1 + len(chunks) / (frequency[term] + 1)) for term in terms}
    ranked = []
    for hit, found in matches:
        present = [term for term, match in found.items() if match]
        if not present:
            continue
        best = max(present, key=lambda term: weights[term])
        start = max(0, found[best].start() - 300)
        ranked.append((sum(weights[term] for term in present), hit, hit.content[start:start + max_chars]))
    ranked.sort(key=lambda row: -row[0])
    return [(hit, text) for _, hit, text in ranked], {
        'searched_chunks': len(chunks), 'full_available_index_searched': True,
        'matched_terms': [term for term in terms if frequency[term]],
        'unmatched_terms': [term for term in terms if not frequency[term]],
        'note': 'Охват индексированного текста, не гарантия полного OCR оригинала. Выдержки не доказывают отсутствие других условий.'}


def extraction_coverage(db, doc):
    """A full text window is independent of whether the original was fully extracted."""
    sources = db.scalars(select(KnowledgeSource).where(KnowledgeSource.organization_id == doc.organization_id,
        KnowledgeSource.document_id == doc.id, KnowledgeSource.status.notin_(['cancelled', 'replaced']))).all()
    reports = []
    current = (doc.data or {}).get('reading')
    if current:
        reports.append({'origin': 'document', **current})
    for source in sources:
        reading = (source.meta or {}).get('reading')
        if reading:
            reports.append({'origin': 'source', 'source_id': str(source.id), **reading})
    complete = False if any(r.get('complete') is False for r in reports) else (
        True if reports and all(r.get('complete') is True for r in reports) else None)
    return {'complete': complete, 'status': 'partial' if complete is False else 'reported_complete' if complete else 'unknown',
            'reports': reports, 'scope': 'Полнота извлечения оценивается отдельно от объёма возвращённого текста. '
            'Reported complete относится к извлечённым сегментам, не ко всему смыслу оригинала.'}
