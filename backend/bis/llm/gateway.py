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

import datetime as dt
import logging
import threading
import time
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
from bis.services.local_config import LOCK

log = logging.getLogger(__name__)


@dataclass
class ProviderEntry:
    provider: LLMProvider
    max_sensitivity: str = "internal"
    mask_pii: bool = False
    capabilities: tuple[str, ...] = ("text", "tools")


@dataclass
class LLMGateway:
    entries: dict[str, ProviderEntry]
    routes: dict[str, list[str]] = field(default_factory=dict)
    on_usage: Callable[[LLMResponse, str], None] | None = None
    health: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Settings) -> LLMGateway:
        entries: dict[str, ProviderEntry] = {}
        for cfg in settings.llm_providers():
            entries[cfg.name] = cls._entry(cfg)
        return cls(entries=entries, routes=settings.llm_routes())

    @staticmethod
    def _entry(cfg: ProviderConfig) -> ProviderEntry:
        return ProviderEntry(provider=build_provider(cfg), max_sensitivity=cfg.max_sensitivity, mask_pii=cfg.mask_pii,
                             capabilities=tuple(cfg.capabilities))

    # --- маршрутизация -------------------------------------------------------------------------
    def has_capability(self, capability: str, task: str = "default", sensitivity: str = "internal") -> list[str]:
        """Провайдеры с нужной возможностью, доступные для задачи и уровня данных (без офлайн-заглушки)."""
        return [n for n, _ in self.candidates(task, sensitivity) if capability in self.entries[n].capabilities
                and getattr(self.entries[n].provider, "model", "") not in ("offline", "")]

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
        if any(m.images for m in messages):
            candidates = [c for c in candidates if "vision" in self.entries[c[0]].capabilities]
            if not candidates:
                raise LLMError("Не подключена модель зрения (провайдер с возможностью vision)", retryable=False)
        if prefer and getattr(self.entries.get(prefer, None), "provider", None) is not None and getattr(self.entries[prefer].provider, "model", "") != "offline":
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
                self.health[name] = {'provider': name, 'ok': False, 'error': str(e)[:400],
                                     'checked_at': dt.datetime.now(dt.UTC).isoformat()}
                log.warning("LLM %s: %s", name, e)
                errors.append(str(e))
                if not e.retryable and len(candidates) == 1:
                    raise
                continue
            if getattr(entry.provider, 'model', '') != 'offline':
                self.health[name] = {'provider': name, 'ok': True, 'checked_at': dt.datetime.now(dt.UTC).isoformat()}
            elif any(getattr(e.provider, 'model', '') not in ('', 'offline') for e in self.entries.values()):
                from bis.llm.providers.offline import NO_MODEL

                reason = ('Облачная модель настроена, но не ответила на этот запрос. Причина: ' + '; '.join(errors)[:700]) if errors else (
                    'Настроенная модель не выбрана для этой задачи: проверьте маршрут и разрешённый уровень данных.')
                reason += '\nИспользован офлайн-режим. Настройки ключа не изменялись.'
                content = response.message.content.replace(NO_MODEL, '').strip()
                response.message = replace(response.message, content=reason + ('\n\n' + content if content else ''))
            if self.on_usage:
                self.on_usage(response, task)
            return response
        raise LLMError("Все провайдеры недоступны: " + "; ".join(errors))

    @staticmethod
    def _call(provider, messages, tools, max_tokens, json_mode, mask):
        from bis.runtime import model_slot

        if getattr(provider, 'model', '') == 'offline':
            return LLMGateway._call_unlimited(provider, messages, tools, max_tokens, json_mode, mask)
        with model_slot():
            return LLMGateway._call_unlimited(provider, messages, tools, max_tokens, json_mode, mask)

    @staticmethod
    def _call_unlimited(
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


@dataclass
class _GatewayBuild:
    revision: int
    event: threading.Event = field(default_factory=threading.Event)
    result: LLMGateway | None = None
    error: Exception | None = None


_gateway: LLMGateway | None = None
_gateway_revision = 0
_gateway_build: _GatewayBuild | None = None


def _construct_gateway(state: _GatewayBuild, settings: Settings):
    global _gateway
    from bis.runtime import model_initializers

    try:
        state.result = LLMGateway.from_settings(settings)
        with LOCK:
            if state.revision == _gateway_revision:
                _gateway = state.result
    except Exception as exc:  # noqa: BLE001 — deliver construction failures to all waiters
        state.error = exc
    finally:
        model_initializers.release()
        state.event.set()


def get_gateway() -> LLMGateway:
    global _gateway_build
    from bis.config import get_settings
    from bis.runtime import model_initializers

    deadline = time.monotonic() + get_settings().model_initialization_wait_seconds
    while True:
        with LOCK:
            if _gateway is not None:
                return _gateway
            if _gateway_build is None:
                if not model_initializers.acquire():
                    raise LLMError('Создание клиентов моделей ещё выполняется. Настройки сохранены; повторите позже.')
                state = _GatewayBuild(_gateway_revision)
                _gateway_build = state
                try:
                    threading.Thread(target=_construct_gateway, args=(state, get_settings()),
                                     daemon=True, name='bis-model-init').start()
                except BaseException:
                    _gateway_build = None
                    model_initializers.release()
                    raise
            state = _gateway_build
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not state.event.wait(remaining):
            raise LLMError('Клиент модели ещё запускается (сертификаты или настройки сети). '
                           'BIS продолжает запуск в фоне; ключ сохранён. Повторите проверку через 30 секунд.')
        with LOCK:
            if state.revision != _gateway_revision:
                continue
            if state.error is not None:
                if _gateway_build is state:
                    _gateway_build = None
                raise state.error
            if _gateway is not None:
                return _gateway


def set_gateway(gateway: LLMGateway | None) -> None:
    global _gateway, _gateway_revision, _gateway_build
    with LOCK:
        _gateway_revision += 1
        _gateway_build = None
        _gateway = gateway
