"""OpenAI-совместимый протокол chat/completions.

Покрывает локальные модели (vLLM, Ollama, LM Studio) и облачные сервисы с совместимым API,
например YandexGPT в Yandex AI Studio (auth_scheme="Api-Key", model="gpt://<folder>/yandexgpt/latest").
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from bis.config import ProviderConfig
from bis.llm.types import LLMError, LLMResponse, Message, ToolCall, ToolSpec


def to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        elif m.role == "assistant" and m.tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                        }
                        for tc in m.tool_calls
                    ],
                }
            )
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def parse_openai_response(data: dict[str, Any], provider: str, model: str) -> LLMResponse:
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"{provider}: неожиданный ответ: {data!r:.300}") from e
    tool_calls: list[ToolCall] = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function", {})
        args = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(args) if isinstance(args, str) else dict(args)
        except json.JSONDecodeError:
            parsed = {"_invalid_json": args}
        tool_calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=parsed))
    finish = choice.get("finish_reason")
    stop = "tool_calls" if tool_calls else ("max_tokens" if finish == "length" else "end")
    if finish == "content_filter":
        stop = "refusal"
    usage = data.get("usage") or {}
    return LLMResponse(
        message=Message(role="assistant", content=msg.get("content") or "", tool_calls=tool_calls),
        stop_reason=stop,
        provider=provider,
        model=data.get("model") or model,
        usage={"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)},
    )


class OpenAICompatProvider:
    def __init__(self, cfg: ProviderConfig, client: httpx.Client | None = None):
        if not cfg.base_url:
            raise ValueError(f"{cfg.name}: не задан base_url")
        self.cfg = cfg
        self.name = cfg.name
        self.model = cfg.model
        headers = {"Content-Type": "application/json"}
        key = cfg.resolved_api_key()
        if key:
            headers["Authorization"] = f"{cfg.auth_scheme} {key}"
        headers.update(cfg.extra.get("headers", {}))
        self.client = client or httpx.Client(
            base_url=cfg.base_url.rstrip("/"), headers=headers, timeout=cfg.timeout, verify=cfg.verify_ssl
        )

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "max_tokens": max_tokens,
        }
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        if tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(self.cfg.extra.get("body", {}))
        try:
            r = self.client.post("/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"{self.name}: сеть: {e}") from e
        if r.status_code >= 400:
            raise LLMError(
                f"{self.name}: HTTP {r.status_code}: {r.text[:300]}",
                retryable=r.status_code in (408, 409, 429) or r.status_code >= 500,
            )
        return parse_openai_response(r.json(), self.name, self.model)
