"""Офлайн-провайдер: работает без языковой модели.

Вызывает поиск по базе знаний и возвращает найденные фрагменты с цитатами (извлекающий ответ).
Нужен для демонстрации, тестов и как последний резерв при недоступности моделей.
"""

from __future__ import annotations

import json
import uuid

from bis.llm.types import LLMResponse, Message, ToolCall, ToolSpec

SEARCH_TOOL = "search_knowledge"
NO_MODEL = (
    "Языковая модель не подключена (офлайн-режим). Доступны поиск по базе знаний, "
    "реестры и формирование документов по формам."
)


class OfflineProvider:
    def __init__(self, name: str = "offline"):
        self.name = name
        self.model = "offline"

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResponse:
        if json_mode:
            return self._reply("{}")
        last_user = max((i for i, m in enumerate(messages) if m.role == "user"), default=-1)
        tool_msgs = [m for m in messages[last_user + 1 :] if m.role == "tool"]
        has_search = any(t.name == SEARCH_TOOL for t in tools or [])
        if has_search and not tool_msgs and last_user >= 0:
            call = ToolCall(id=f"off_{uuid.uuid4().hex[:8]}", name=SEARCH_TOOL, arguments={"query": messages[last_user].content})
            return LLMResponse(
                message=Message(role="assistant", tool_calls=[call]), stop_reason="tool_calls", provider=self.name, model=self.model
            )
        if tool_msgs:
            return self._reply(self._summarize(tool_msgs[-1].content))
        return self._reply(NO_MODEL)

    def _reply(self, text: str) -> LLMResponse:
        return LLMResponse(message=Message(role="assistant", content=text), stop_reason="end", provider=self.name, model=self.model)

    @staticmethod
    def _summarize(content: str) -> str:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return content[:2000]
        results = data.get("results") if isinstance(data, dict) else None
        if not results:
            return "В базе знаний ничего не найдено по запросу. " + NO_MODEL
        lines = ["Найденные положения (офлайн-режим, ответ без языковой модели — проверьте применимость):", ""]
        for r in results[:5]:
            text = " ".join(str(r.get("content", "")).split())
            if len(text) > 500:
                text = text[:500] + "…"
            lines.append(f"[{r.get('n')}] {r.get('citation')}: {text}")
        return "\n".join(lines)


class ScriptedProvider:
    """Провайдер для тестов: возвращает заранее заданные ответы и запоминает запросы."""

    def __init__(self, responses: list[LLMResponse | Message | str] | None = None, name: str = "scripted"):
        self.name = name
        self.model = "scripted"
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> LLMResponse:
        self.calls.append({"messages": list(messages), "tools": tools, "json_mode": json_mode})
        item = self.responses.pop(0) if self.responses else "OK"
        if isinstance(item, LLMResponse):
            return item
        if isinstance(item, str):
            item = Message(role="assistant", content=item)
        stop = "tool_calls" if item.tool_calls else "end"
        return LLMResponse(message=item, stop_reason=stop, provider=self.name, model=self.model)
