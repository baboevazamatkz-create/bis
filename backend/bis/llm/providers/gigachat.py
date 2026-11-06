"""GigaChat (Сбер). OAuth-токен по ключу авторизации, вызов функций через `functions`.

Сертификаты: API использует корневой сертификат НУЦ Минцифры — укажите путь к нему в verify_ssl.
extra: {"scope": "GIGACHAT_API_PERS|GIGACHAT_API_B2B|GIGACHAT_API_CORP", "oauth_url": "..."}
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx

from bis.config import ProviderConfig
from bis.llm.types import LLMError, LLMResponse, Message, ToolCall, ToolSpec

DEFAULT_BASE_URL = "https://gigachat.devices.sberbank.ru/api/v1"
DEFAULT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"


def to_gigachat_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            out.append({"role": "function", "name": m.name or "", "content": m.content})
        elif m.role == "assistant" and m.tool_calls:
            tc = m.tool_calls[0]  # GigaChat возвращает не более одного вызова за ход
            item: dict[str, Any] = {
                "role": "assistant",
                "content": m.content or "",
                "function_call": {"name": tc.name, "arguments": tc.arguments},
            }
            if isinstance(m.raw, dict) and m.raw.get("functions_state_id"):
                item["functions_state_id"] = m.raw["functions_state_id"]
            out.append(item)
        else:
            out.append({"role": m.role, "content": m.content})
    return out


class GigaChatProvider:
    def __init__(self, cfg: ProviderConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self.name = cfg.name
        self.model = cfg.model or "GigaChat-2-Max"
        self.base_url = (cfg.base_url or DEFAULT_BASE_URL).rstrip("/")
        self.oauth_url = cfg.extra.get("oauth_url", DEFAULT_OAUTH_URL)
        self.scope = cfg.extra.get("scope", "GIGACHAT_API_CORP")
        self.client = client or httpx.Client(timeout=cfg.timeout, verify=cfg.verify_ssl)
        self._token: str | None = None
        self._token_exp: float = 0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        key = self.cfg.resolved_api_key()
        if not key:
            raise LLMError(f"{self.name}: не задан ключ авторизации", retryable=False)
        try:
            r = self.client.post(
                self.oauth_url,
                headers={
                    "Authorization": f"Basic {key}",
                    "RqUID": str(uuid.uuid4()),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={"scope": self.scope},
            )
        except httpx.HTTPError as e:
            raise LLMError(f"{self.name}: OAuth сеть: {e}") from e
        if r.status_code >= 400:
            raise LLMError(f"{self.name}: OAuth HTTP {r.status_code}: {r.text[:200]}", retryable=r.status_code >= 500)
        data = r.json()
        self._token = data["access_token"]
        exp = data.get("expires_at", 0)
        self._token_exp = exp / 1000 if exp > 10**11 else (exp or time.time() + 1500)
        return self._token

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
            "messages": to_gigachat_messages(messages),
            "max_tokens": max_tokens,
        }
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        if tools:
            body["functions"] = [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools]
            body["function_call"] = "auto"
        try:
            r = self.client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._access_token()}", "Accept": "application/json"},
                json=body,
            )
        except httpx.HTTPError as e:
            raise LLMError(f"{self.name}: сеть: {e}") from e
        if r.status_code == 401:
            self._token = None
        if r.status_code >= 400:
            raise LLMError(
                f"{self.name}: HTTP {r.status_code}: {r.text[:300]}",
                retryable=r.status_code in (401, 408, 429) or r.status_code >= 500,
            )
        data = r.json()
        choice = data["choices"][0]
        msg = choice["message"]
        tool_calls: list[ToolCall] = []
        fc = msg.get("function_call")
        if fc:
            args = fc.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_invalid_json": args}
            tool_calls.append(ToolCall(id=f"fc_{uuid.uuid4().hex[:8]}", name=fc.get("name", ""), arguments=args))
        finish = choice.get("finish_reason")
        stop = "tool_calls" if tool_calls else {"length": "max_tokens", "blacklist": "refusal"}.get(finish, "end")
        usage = data.get("usage") or {}
        return LLMResponse(
            message=Message(
                role="assistant",
                content=msg.get("content") or "",
                tool_calls=tool_calls,
                raw={"functions_state_id": msg.get("functions_state_id")},
                raw_provider=self.name,
            ),
            stop_reason=stop,
            provider=self.name,
            model=data.get("model") or self.model,
            usage={"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)},
        )
