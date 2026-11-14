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


class OCRError(Exception):
    pass


def available() -> bool:
    s = get_settings()
    if s.ocr_provider == "yandex":
        return all(s.yandex())
    if s.ocr_provider == "anthropic":
        try:
            from bis.llm.gateway import get_gateway

            gateway = get_gateway()
            return bool(gateway.has_capability("vision", "vision"))
        except Exception:  # noqa: BLE001 — состояние OCR не должно ломать чтение документов
            return False
    return False


def _anthropic_images(data: bytes, mime: str | None) -> list[dict[str, str]]:
    """Convert document pages into Anthropic-supported PNG/JPEG image blocks."""
    from PIL import Image, ImageSequence

    mime = (mime or "").lower()
    images: list[Image.Image] = []
    if "pdf" in mime:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        for index in range(len(pdf)):
            page = pdf[index]
            width, height = page.get_size()
            images.append(page.render(scale=1600 / max(width, height)).to_pil().convert("RGB"))
    else:
        with Image.open(io.BytesIO(data)) as image:
            images = [frame.copy() for frame in ImageSequence.Iterator(image)]

    out: list[dict[str, str]] = []
    for image in images:
        if max(image.size) > 2048:
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        # PNG preserves fine linework; other inputs are normalized to universally supported JPEG.
        if mime == "image/png" and image.mode in ("RGB", "RGBA"):
            image.save(buf, format="PNG", optimize=True)
            kind = "image/png"
        else:
            image.convert("RGB").save(buf, format="JPEG", quality=92, optimize=True)
            kind = "image/jpeg"
        if buf.tell() > 4_500_000:
            buf = io.BytesIO()
            image.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
            kind = "image/jpeg"
        out.append({"mime": kind, "data": base64.b64encode(buf.getvalue()).decode("ascii")})
    return out


def _anthropic_recognize_pages(data: bytes, mime: str | None) -> list[str]:
    from bis.llm.gateway import get_gateway
    from bis.llm.types import LLMError, Message

    images = _anthropic_images(data, mime)
    if not images:
        raise OCRError("Claude Vision не получил изображение страницы")
    gateway = get_gateway()
    prompt = ("Выполни точное OCR этой страницы строительного документа. Перепиши все видимые надписи "
              "по порядку чтения, сохраняя номера, единицы измерения и строки таблиц через разделитель |. "
              "Не пересказывай и не делай выводов. Неразборчивые места помечай [неразборчиво]. "
              "Не добавляй текст, которого нет на изображении.")
    def read(image: dict[str, str]) -> str:
        try:
            response = gateway.complete(
                [Message(role="user", content=prompt, images=[image])],
                task="vision", sensitivity="internal", max_tokens=8192,
            )
        except LLMError as exc:
            raise OCRError(f"Claude Vision OCR: {exc}") from exc
        return response.message.content.strip()

    with ThreadPoolExecutor(max_workers=min(4, len(images))) as pool:
        return list(pool.map(read, images))


def _pdf_pages(data: bytes) -> list[bytes]:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    pages = list(reader.pages)
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
    if s.ocr_provider == "anthropic":
        if not available():
            raise OCRError("Claude Vision не настроен: выберите Anthropic и модель с поддержкой изображений")
        return _anthropic_recognize_pages(data, mime)
    if s.ocr_provider != "yandex":
        raise OCRError("OCR не подключён")
    folder, key = s.yandex()
    if not key:
        raise OCRError("Не задан API-ключ Yandex Cloud для OCR")
    if not folder:
        raise OCRError("Не задан каталог Yandex Cloud (BIS_YANDEX_FOLDER_ID)")
    mime = (mime or "").lower()
    if "pdf" in mime:
        items = [("application/pdf", p) for p in _pdf_pages(data)]
    elif any(kind in mime for kind in ('tiff', 'bmp', 'gif', 'webp')):
        from PIL import Image, ImageSequence

        with Image.open(io.BytesIO(data)) as image:
            items = []
            for frame in ImageSequence.Iterator(image):
                buf = io.BytesIO()
                frame.convert('RGB').save(buf, format='JPEG')
                items.append(('image/jpeg', buf.getvalue()))
    elif "png" in mime:
        items = [("image/png", data)]
    else:
        items = [("image/jpeg", data)]
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
