"""Распознавание сканов и фото документов (OCR): Yandex Vision OCR.

Сканы PDF без текстового слоя и фото накладных, сертификатов, протоколов превращаются в текст,
который дальше читает чтец документов. Ключ AI Studio, роль ai.vision.user.
"""

from __future__ import annotations

import base64
import io

import httpx

from bis.config import get_settings

YANDEX_OCR = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"
MAX_PAGES = 20


class OCRError(Exception):
    pass


def available() -> bool:
    s = get_settings()
    return s.ocr_provider == "yandex" and bool(s.yandex()[0])


def _pdf_pages(data: bytes) -> list[bytes]:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    out = []
    for page in reader.pages[:MAX_PAGES]:
        w = PdfWriter()
        w.add_page(page)
        buf = io.BytesIO()
        w.write(buf)
        out.append(buf.getvalue())
    return out


def recognize(data: bytes, mime: str | None, client: httpx.Client | None = None) -> str:
    """Текст документа. Многостраничный PDF распознаётся по страницам (синхронный API — одна страница)."""
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
    http = client or httpx.Client(timeout=90)
    texts = []
    for kind, content in items:
        try:
            r = http.post(YANDEX_OCR, headers={"Authorization": f"Api-Key {key or 'proxy-injected'}", "x-folder-id": folder},
                          json={"mimeType": kind, "languageCodes": ["ru", "en"], "model": "page",
                                "content": base64.b64encode(content).decode()})
        except httpx.HTTPError as e:
            raise OCRError(f"Vision OCR недоступен: {e}") from e
        if r.status_code >= 400:
            raise OCRError(f"Vision OCR: HTTP {r.status_code}: {r.text[:200]}")
        texts.append(((r.json().get("result") or {}).get("textAnnotation") or {}).get("fullText", ""))
    return "\n\n".join(t.strip() for t in texts if t.strip())
