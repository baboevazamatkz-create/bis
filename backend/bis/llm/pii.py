"""Обнаружение и обратимое маскирование персональных данных (152-ФЗ) перед отправкой во внешние модели.

Реквизиты юрлиц (ИНН 10 знаков, ОГРН) — публичные сведения ЕГРЮЛ и не маскируются.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("SNILS", re.compile(r"\b\d{3}-\d{3}-\d{3}[ -]\d{2}\b")),
    ("PASSPORT", re.compile(r"(?i)паспорт\w*[^\d]{0,20}(?P<v>\b\d{2}\s?\d{2}\s?№?\s?\d{6})\b")),
    ("ACCOUNT", re.compile(r"(?<!\d)\d{20}(?!\d)")),
    ("INN_PERSON", re.compile(r"(?<!\d)\d{12}(?!\d)")),
    ("PHONE", re.compile(r"(?<![\w\d])(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")),
]


def detect_pii(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            value = m.group("v") if "v" in pattern.groupindex else m.group(0)
            found.append((kind, value))
    return found


def contains_pii(text: str) -> bool:
    return any(p.search(text) for _, p in _PATTERNS)


@dataclass
class PiiMasker:
    """Заменяет ПДн на метки ⟦PHONE_1⟧ и восстанавливает их в ответе модели."""

    mapping: dict[str, str] = field(default_factory=dict)  # метка -> исходное значение
    _reverse: dict[str, str] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict)

    def mask(self, text: str) -> str:
        if not text:
            return text
        for kind, pattern in _PATTERNS:
            text = pattern.sub(lambda m, k=kind: self._replace(k, m), text)
        return text

    def _replace(self, kind: str, m: re.Match[str]) -> str:
        if "v" in m.re.groupindex and m.group("v"):
            start, end = m.span("v")
            base = m.start()
            whole = m.group(0)
            return whole[: start - base] + self._token(kind, m.group("v")) + whole[end - base :]
        return self._token(kind, m.group(0))

    def _token(self, kind: str, value: str) -> str:
        if value in self._reverse:
            return self._reverse[value]
        self._counters[kind] = self._counters.get(kind, 0) + 1
        token = f"⟦{kind}_{self._counters[kind]}⟧"
        self.mapping[token] = value
        self._reverse[value] = token
        return token

    def unmask(self, text: str) -> str:
        if not text or not self.mapping:
            return text
        for token, value in self.mapping.items():
            text = text.replace(token, value)
        return text

    def unmask_obj(self, obj):
        if isinstance(obj, str):
            return self.unmask(obj)
        if isinstance(obj, list):
            return [self.unmask_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.unmask_obj(v) for k, v in obj.items()}
        return obj

    def mask_obj(self, obj):
        if isinstance(obj, str):
            return self.mask(obj)
        if isinstance(obj, list):
            return [self.mask_obj(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self.mask_obj(v) for k, v in obj.items()}
        return obj
