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
    monkeypatch.setattr(get_settings(), "ocr_provider", "yandex")
    monkeypatch.setattr(ocr.time, "sleep", lambda s: None)
    lock, state = threading.Lock(), {"now": 0, "max": 0, "calls": 0, "limited": False}
    sizes = {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["mimeType"] == "application/pdf"
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


def test_anthropic_ocr_routes_every_page_through_vision(monkeypatch):
    from bis import ocr
    from bis.config import get_settings
    from bis.llm.types import LLMResponse, Message

    class Gateway:
        def __init__(self):
            self.pages = []

        def complete(self, messages, **kwargs):
            assert kwargs['task'] == 'vision'
            page = messages[0].images[0]['data']
            self.pages.append(page)
            return LLMResponse(Message(role='assistant', content=f"текст {page}"),
                               stop_reason='end', provider='anthropic-claude', model='claude-opus-5-5')

    gateway = Gateway()
    monkeypatch.setattr(get_settings(), 'ocr_provider', 'anthropic')
    monkeypatch.setattr(ocr, 'available', lambda: True)
    monkeypatch.setattr(ocr, '_anthropic_images', lambda data, mime: [
        {'mime': 'image/jpeg', 'data': 'page-a'}, {'mime': 'image/jpeg', 'data': 'page-b'}])
    monkeypatch.setattr('bis.llm.gateway.get_gateway', lambda: gateway)
    assert ocr.recognize_pages(b'fixture', 'application/pdf') == ['текст page-a', 'текст page-b']
    assert set(gateway.pages) == {'page-a', 'page-b'}
