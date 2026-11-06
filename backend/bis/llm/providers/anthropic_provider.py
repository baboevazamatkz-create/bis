"""Claude через официальный Anthropic SDK (там, где сервис доступен легально).

- Адаптивное мышление включено по умолчанию у текущих моделей; глубина задаётся effort.
- Серверный fallback при отказе модели (`fallbacks="default"`).
- Нативные блоки ответа (thinking, tool_use) передаются обратно без изменений внутри одного цикла агента.
"""

from __future__ import annotations

from typing import Any

from bis.config import ProviderConfig
from bis.llm.types import LLMError, LLMResponse, Message, ToolCall, ToolSpec, split_system

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def to_anthropic_messages(messages: list[Message], provider_name: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            # Все результаты инструментов одного хода — в одном сообщении user.
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages:
        if m.role == "tool":
            pending_results.append(
                {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content, "is_error": m.is_error}
            )
            continue
        flush_results()
        if m.role == "assistant":
            if m.raw is not None and m.raw_provider == provider_name:
                content: Any = m.raw
            else:
                content = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                content.extend(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments} for tc in m.tool_calls
                )
                if not content:
                    content = [{"type": "text", "text": "…"}]
            out.append({"role": "assistant", "content": content})
        elif m.role == "user":
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], str):
                out[-1]["content"] += "\n\n" + m.content
            else:
                out.append({"role": "user", "content": m.content})
    flush_results()
    return out


def parse_anthropic_response(response: Any, provider_name: str) -> LLMResponse:
    stop_reason = getattr(response, "stop_reason", None)
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    if stop_reason != "refusal":
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                texts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))
    if stop_reason == "refusal":
        stop = "refusal"
    elif tool_calls:
        stop = "tool_calls"
    elif stop_reason == "max_tokens":
        stop = "max_tokens"
    else:
        stop = "end"
    usage = getattr(response, "usage", None)
    return LLMResponse(
        message=Message(
            role="assistant",
            content="\n".join(texts).strip(),
            tool_calls=tool_calls,
            raw=None if stop == "refusal" else response.content,
            raw_provider=provider_name,
        ),
        stop_reason=stop,
        provider=provider_name,
        model=getattr(response, "model", "") or "",
        usage={
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        },
    )


class AnthropicProvider:
    def __init__(self, cfg: ProviderConfig, client: Any = None):
        self.cfg = cfg
        self.name = cfg.name
        self.model = cfg.model or DEFAULT_MODEL
        if client is None:
            import anthropic

            kwargs: dict[str, Any] = {"timeout": cfg.timeout, "max_retries": 2}
            key = cfg.resolved_api_key()
            if key:
                kwargs["api_key"] = key
            if cfg.base_url:
                kwargs["base_url"] = cfg.base_url
            client = anthropic.Anthropic(**kwargs)
        self.client = client

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 16000,
        json_mode: bool = False,
    ) -> LLMResponse:
        import anthropic

        system, rest = split_system(messages)
        if json_mode:
            system += "\n\nОтвет — только валидный JSON без пояснений."
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max(max_tokens, 1024),
            "messages": to_anthropic_messages(rest, self.name),
            "output_config": {"effort": self.cfg.effort or "medium"},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        try:
            response = self.client.beta.messages.create(**params)
        except anthropic.BadRequestError as e:
            raise LLMError(f"{self.name}: запрос отклонён: {e.message}", retryable=False) from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
            raise LLMError(f"{self.name}: доступ: {e.message}", retryable=False) from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"{self.name}: превышен лимит запросов", retryable=True) from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"{self.name}: HTTP {e.status_code}", retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"{self.name}: сеть: {e}", retryable=True) from e
        return parse_anthropic_response(response, self.name)
