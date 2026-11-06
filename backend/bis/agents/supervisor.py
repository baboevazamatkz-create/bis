"""Супервизор: определяет, какой агент обработает запрос.

1. Явный выбор пользователя.
2. Быстрые правила (ключевые слова агентов) — без вызова модели.
3. При неоднозначности — классификация моделью (задача route, дешёвая/локальная модель).
4. По умолчанию — «Помощник».
"""

from __future__ import annotations

import json
import re

from bis.agents.base import Agent
from bis.llm.gateway import LLMGateway
from bis.llm.types import LLMError, Message

DEFAULT_AGENT = "assistant"


class Supervisor:
    def __init__(self, agents: dict[str, Agent], default: str = DEFAULT_AGENT):
        self.agents = agents
        self.default = default if default in agents else next(iter(agents))

    def route(self, text: str, gateway: LLMGateway | None = None, explicit: str | None = None) -> tuple[str, str]:
        if explicit:
            if explicit not in self.agents:
                raise LookupError(f"Агент «{explicit}» не найден")
            return explicit, "выбран пользователем"
        scores: dict[str, int] = {}
        low = text.lower()
        for name, agent in self.agents.items():
            if agent.keywords:
                n = len(re.findall(agent.keywords, low))
                if n:
                    scores[name] = n
        if scores:
            best = max(scores.values())
            top = sorted((n for n, s in scores.items() if s == best), key=lambda n: -self.agents[n].priority)
            if len(top) == 1:
                return top[0], "по ключевым словам"
            chosen = self._llm_route(text, gateway, top)
            return (chosen, "моделью") if chosen else (top[0], "по ключевым словам (неоднозначно)")
        chosen = self._llm_route(text, gateway, list(self.agents))
        return (chosen, "моделью") if chosen else (self.default, "по умолчанию")

    def _llm_route(self, text: str, gateway: LLMGateway | None, candidates: list[str]) -> str | None:
        if len(candidates) == 1:
            return candidates[0]
        if gateway is None or not candidates:
            return None
        catalog = "\n".join(f"- {n}: {self.agents[n].title} — {self.agents[n].description}" for n in candidates)
        prompt = [
            Message(role="system", content="Ты маршрутизатор запросов. Выбери одного агента для запроса пользователя. "
                    'Ответь JSON вида {"agent": "<имя>"} без пояснений.\nАгенты:\n' + catalog),
            Message(role="user", content=text[:4000]),
        ]
        try:
            resp = gateway.complete(prompt, task="route", json_mode=True, max_tokens=200)
        except LLMError:
            return None
        match = re.search(r"\{.*\}", resp.message.content, re.DOTALL)
        if not match:
            return None
        try:
            name = json.loads(match.group(0)).get("agent")
        except (json.JSONDecodeError, AttributeError):
            return None
        return name if name in candidates else None
