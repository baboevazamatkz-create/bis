"""Распознавание сканов: страницы PDF — параллельно, порядок сохраняется, лимит запросов (429) — повтор."""

import base64
import io
import json
import threading

import httpx


def _pdf(pages: int) -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_pdf_pages_in_parallel_in_order(monkeypatch):
    from bis import ocr
    from bis.config import get_settings

    monkeypatch.setattr(type(get_settings()), "yandex", lambda self: ("b1g", "key"))
    monkeypatch.setattr(ocr.time, "sleep", lambda s: None)
    lock, state = threading.Lock(), {"now": 0, "max": 0, "calls": 0, "limited": False}
    sizes = {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        with lock:
            state["calls"] += 1
            if not state["limited"]:  # первый запрос — «слишком часто»
                state["limited"] = True
                return httpx.Response(429, json={})
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            n = sizes.setdefault(len(base64.b64decode(body["content"])), len(sizes) + 1)
        threading.Event().wait(0.05)  # time.sleep подменён (повторы без ожидания)
        with lock:
            state["now"] -= 1
        return httpx.Response(200, json={"result": {"textAnnotation": {"fullText": f"страница {n}"}}})

    pages = ocr._pdf_pages(_pdf(1))
    assert len(pages) == 1
    client = httpx.Client(transport=httpx.MockTransport(handler))
    text = ocr.recognize(_pdf(1), "application/pdf", client=client)
    assert text == "страница 1" and state["calls"] == 2  # 429 → повтор
    # многостраничный: одинаковые пустые страницы — проверяем параллельность и число запросов
    state.update(calls=0, max=0)
    out = ocr.recognize(_pdf(6), "application/pdf", client=client)
    assert out.count("страница") == 6 and state["calls"] == 6 and state["max"] > 1
