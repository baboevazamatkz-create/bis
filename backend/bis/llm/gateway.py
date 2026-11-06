"""LLM-шлюз: гибридная маршрутизация между локальными и облачными моделями.

Правила:
1. Каждой задаче (route) сопоставлен упорядоченный список провайдеров; первый доступный — основной,
   остальные — резерв при сбое.
2. Провайдер получает данные только до своего уровня `max_sensitivity`
   (public < internal < confidential). Конфиденциальное уходит только в закрытый контур.
3. Если уровень «confidential» определён автоматически по ПДн, а провайдер умеет маскировать ПДн,
   ПДн заменяются метками и данные считаются «internal».
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from bis.config import ProviderConfig, Settings
from bis.llm.pii import PiiMasker, contains_pii
from bis.llm.providers import build_provider
from bis.llm.types import (
    SENSITIVITY_LEVELS,
    LLMError,
    LLMProvider,
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)

log = logging.getLogger(__name__)


@dataclass
class ProviderEntry:
    provider: LLMProvider
    max_sensitivity: str = "internal"
    mask_pii: bool = False


@dataclass
class LLMGateway:
    entries: dict[str, ProviderEntry]
    routes: dict[str, list[str]] = field(default_factory=dict)
    on_usage: Callable[[LLMResponse, str], None] | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> LLMGateway:
        entries: dict[str, ProviderEntry] = {}
        for cfg in settings.llm_providers():
            entries[cfg.name] = cls._entry(cfg)
        return cls(entries=entries, routes=settings.llm_routes())

    @staticmethod
    def _entry(cfg: ProviderConfig) -> ProviderEntry:
        return ProviderEntry(provider=build_provider(cfg), max_sensitivity=cfg.max_sensitivity, mask_pii=cfg.mask_pii)

    # --- маршрутизация -------------------------------------------------------------------------
    def candidates(self, task: str, sensitivity: str, *, auto_detected: bool = False) -> list[tuple[str, bool]]:
        """Возвращает [(имя провайдера, нужно_ли_маскировать)] в порядке приоритета."""
        names = self.routes.get(task) or self.routes.get("default") or list(self.entries)
        level = SENSITIVITY_LEVELS[sensitivity]
        result: list[tuple[str, bool]] = []
        for name in names:
            entry = self.entries.get(name)
            if entry is None:
                continue
            allowed = SENSITIVITY_LEVELS[entry.max_sensitivity]
            if level <= allowed:
                result.append((name, entry.mask_pii and sensitivity != "public"))
            elif (
                auto_detected
                and sensitivity == "confidential"
                and entry.mask_pii
                and allowed >= SENSITIVITY_LEVELS["internal"]
            ):
                result.append((name, True))
        return result

    def complete(
        self,
        messages: list[Message],
        *,
        task: str = "chat",
        sensitivity: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        json_mode: bool = False,
        prefer: str | None = None,
    ) -> LLMResponse:
        auto = sensitivity is None
        if sensitivity is None:
            text = "\n".join(m.content for m in messages)
            sensitivity = "confidential" if contains_pii(text) else "internal"
        elif sensitivity != "confidential" and contains_pii("\n".join(m.content for m in messages)):
            # ПДн в данных повышают уровень, но маскирование допустимо
            sensitivity, auto = "confidential", True

        candidates = self.candidates(task, sensitivity, auto_detected=auto)
        if prefer:
            candidates.sort(key=lambda c: c[0] != prefer)
        if not candidates:
            raise LLMError(
                f"Нет провайдера, которому разрешены данные уровня «{sensitivity}» для задачи «{task}». "
                "Подключите локальную модель (закрытый контур).",
                retryable=False,
            )
        errors: list[str] = []
        for name, mask in candidates:
            entry = self.entries[name]
            try:
                response = self._call(entry.provider, messages, tools, max_tokens, json_mode, mask)
            except LLMError as e:
                log.warning("LLM %s: %s", name, e)
                errors.append(str(e))
                if not e.retryable and len(candidates) == 1:
                    raise
                continue
            if self.on_usage:
                self.on_usage(response, task)
            return response
        raise LLMError("Все провайдеры недоступны: " + "; ".join(errors))

    @staticmethod
    def _call(
        provider: LLMProvider,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        max_tokens: int,
        json_mode: bool,
        mask: bool,
    ) -> LLMResponse:
        if not mask:
            return provider.complete(messages, tools=tools, max_tokens=max_tokens, json_mode=json_mode)
        masker = PiiMasker()
        masked = [
            replace(
                m,
                content=masker.mask(m.content),
                tool_calls=[replace(tc, arguments=masker.mask_obj(tc.arguments)) for tc in m.tool_calls],
                # нативные блоки могут содержать ПДн — при маскировании не передаём их
                raw=None,
                raw_provider=None,
            )
            for m in messages
        ]
        response = provider.complete(masked, tools=tools, max_tokens=max_tokens, json_mode=json_mode)
        msg = response.message
        response.message = replace(
            msg,
            content=masker.unmask(msg.content),
            tool_calls=[ToolCall(tc.id, tc.name, masker.unmask_obj(tc.arguments)) for tc in msg.tool_calls],
            raw=None,
            raw_provider=None,
        )
        return response


_gateway: LLMGateway | None = None


def get_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        from bis.config import get_settings

        _gateway = LLMGateway.from_settings(get_settings())
    return _gateway


def set_gateway(gateway: LLMGateway | None) -> None:
    global _gateway
    _gateway = gateway
