"""Проверка подключения моделей: отвечает ли каждый провайдер, умеет ли JSON, сколько думает."""

from __future__ import annotations

import time
from typing import Any

from bis.llm.extract import parse_json
from bis.llm.gateway import LLMGateway
from bis.llm.types import LLMError, Message

HINTS = [
    ("PermissionDenied", "Нет прав на модель: выдайте сервисному аккаунту ключа роль ai.languageModels.user в каталоге"),
    ("Permission denied", "Нет прав: выдайте сервисному аккаунту роль ai.languageModels.user в каталоге"),
    ("not found or invalid", "Ключ не принят: проверьте, что сохранён секретный ключ целиком (для AI Studio — строка AQVN…)"),
    ("Failed to authenticate", "Ключ не принят: проверьте секретный ключ"),
    ("Failed to parse model URI", "Неверный адрес модели: gpt://<каталог>/<модель>/latest, проверьте YANDEX_FOLDER_ID"),
    ("does not match with service account folder", "Каталог в адресе модели не совпадает с каталогом сервисного аккаунта ключа"),
    ("сеть", "Нет связи с сервисом: проверьте сеть и адрес base_url"),
]


def hint_for(error: str) -> str | None:
    return next((h for key, h in HINTS if key in error), None)


def check_providers(gateway: LLMGateway) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, entry in gateway.entries.items():
        model = getattr(entry.provider, "model", "")
        if model in ("offline", ""):
            continue
        routes = [task for task, names in gateway.routes.items() if name in names]
        started = time.monotonic()
        try:
            r = entry.provider.complete([Message(role="user", content="Ответь одним словом: столица России?")], max_tokens=16)
            seconds = round(time.monotonic() - started, 1)
            json_ok = None
            try:
                j = entry.provider.complete([Message(role="system", content="Отвечай только JSON."),
                                             Message(role="user", content='Верни {"ok": true}')],
                                            max_tokens=30, json_mode=True)
                json_ok = parse_json(j.message.content) is not None
            except LLMError:
                json_ok = False
            out.append({"provider": name, "model": model, "ok": True, "reply": r.message.content.strip()[:80],
                        "json": json_ok, "seconds": seconds, "routes": routes,
                        "capabilities": list(entry.capabilities)})
        except LLMError as e:
            out.append({"provider": name, "model": model, "ok": False, "error": str(e)[:400], "hint": hint_for(str(e)),
                        "routes": routes, "capabilities": list(entry.capabilities)})
    return out
