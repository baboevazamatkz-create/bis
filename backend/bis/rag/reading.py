"""Extraction coverage shared by upload, indexing, rereading and agent tools."""
from bis.rag.parsers import text_quality


def reading_report(pages, method):
    empty = [p.number for p in pages if p.number is not None and not p.text.strip()]
    sparse = [p.number for p in pages if p.number is not None and len(p.text.strip()) < 50]
    unread = [i for i, page in enumerate(pages, 1) if not page.text.strip()]
    low_quality = [i for i, page in enumerate(pages, 1) if page.text.strip() and text_quality(page.text) < 0.5]
    warnings = list(dict.fromkeys(page.warning for page in pages if page.warning))
    return {'segments': len(pages), 'with_text': sum(bool(page.text.strip()) for page in pages),
            'sparse_pages': sparse, 'empty_pages': empty, 'unread_segments': unread,
            'low_quality_segments': low_quality,
            'complete': bool(pages) and not (sparse or unread or low_quality or warnings),
            'method': method, 'warnings': warnings,
            'scope': 'Охват извлечённых сегментов; не подтверждение полноты инженерного понимания или OCR оригинала.'}
