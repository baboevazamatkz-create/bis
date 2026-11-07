"""Нейтральные к провайдеру типы сообщений, инструментов и ответов."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]
StopReason = Literal["end", "tool_calls", "max_tokens", "refusal"]

SENSITIVITY_LEVELS = {"public": 0, "internal": 1, "confidential": 2}


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # имя инструмента для role=tool
    is_error: bool = False
    # Нативное содержимое ответа провайдера (например, блоки thinking у Claude).
    # Передаётся обратно без изменений, если история уходит тому же провайдеру.
    raw: Any = None
    raw_provider: str | None = None
    # Изображения для моделей зрения: [{"mime": "image/jpeg", "data": "<base64>"}]
    images: list[dict[str, str]] = field(default_factory=list)


@dataclass
class LLMResponse:
    message: Message
    stop_reason: StopReason
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)


class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResponse: ...


def split_system(messages: list[Message]) -> tuple[str, list[Message]]:
    system = "\n\n".join(m.content for m in messages if m.role == "system" and m.content)
    return system, [m for m in messages if m.role != "system"]
