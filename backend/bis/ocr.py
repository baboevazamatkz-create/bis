"""Распознавание сканов и фото документов (OCR): Yandex Vision OCR.

Сканы PDF без текстового слоя и фото накладных, сертификатов, протоколов превращаются в текст,
который дальше читает чтец документов. Ключ AI Studio, роль ai.vision.user.
"""

from __future__ import annotations

import base64
import io
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from bis.config import get_settings

YANDEX_OCR = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"
MAX_PAGES = 300  # длинный скан: первые 200 и последние 100 страниц (реквизиты сторон — в конце)


class OCRError(Exception):
    pass


def available() -> bool:
    s = get_settings()
    return s.ocr_provider == "yandex" and bool(s.yandex()[0])


def _pdf_pages(data: bytes) -> list[bytes]:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    pages = list(reader.pages)
    if len(pages) > MAX_PAGES:
        pages = pages[:200] + pages[-100:]
    out = []
    for page in pages:
        w = PdfWriter()
        w.add_page(page)
        buf = io.BytesIO()
        w.write(buf)
        out.append(buf.getvalue())
    return out


def recognize(data: bytes, mime: str | None, client: httpx.Client | None = None) -> str:
    """Текст документа целиком."""
    return "\n\n".join(t.strip() for t in recognize_pages(data, mime, client) if t.strip())


def recognize_pages(data: bytes, mime: str | None, client: httpx.Client | None = None) -> list[str]:
    """Текст по страницам (порядок сохраняется). Многостраничный PDF — по странице на запрос (синхронный API)."""
    s = get_settings()
    folder, key = s.yandex()
    if not folder:
        raise OCRError("Не задан каталог Yandex Cloud (BIS_YANDEX_FOLDER_ID)")
    mime = (mime or "").lower()
    if "pdf" in mime:
        items = [("PDF", p) for p in _pdf_pages(data)]
    elif "png" in mime:
        items = [("PNG", data)]
    else:
        items = [("JPEG", data)]
    http = client or _client()

    def page(item: tuple[str, bytes]) -> str:
        kind, content = item
        for attempt in range(4):
            try:
                r = http.post(YANDEX_OCR, headers={"Authorization": f"Api-Key {key or 'proxy-injected'}", "x-folder-id": folder},
                              json={"mimeType": kind, "languageCodes": ["ru", "en"], "model": "page",
                                    "content": base64.b64encode(content).decode()})
            except httpx.HTTPError as e:
                raise OCRError(f"Vision OCR недоступен: {e}") from e
            if r.status_code == 429 or r.status_code >= 500:  # лимит запросов — подождать и повторить
                time.sleep(1 + attempt * 2)
                continue
            if r.status_code >= 400:
                raise OCRError(f"Vision OCR: HTTP {r.status_code}: {r.text[:200]}")
            return ((r.json().get("result") or {}).get("textAnnotation") or {}).get("fullText", "")
        raise OCRError("Vision OCR: превышен лимит запросов, повторите позже")

    # страницы — параллельно (по 4): скан на 20 страниц распознаётся в разы быстрее, порядок страниц сохраняется
    if len(items) == 1:
        texts = [page(items[0])]
    else:
        with ThreadPoolExecutor(max_workers=6) as pool:
            texts = list(pool.map(page, items))
    return texts


_HTTP: httpx.Client | None = None


def _client() -> httpx.Client:
    """Одно соединение на процесс (без нового TLS-рукопожатия на каждую страницу)."""
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.Client(timeout=90, limits=httpx.Limits(max_connections=8))
    return _HTTP
