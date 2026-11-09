"""Офлайн-провайдер: работает без языковой модели.

Распознаёт типовые запросы по ключевым словам и вызывает инструменты агента по сценарию
(просрочки, план-факт, остатки, М-29, закрытие, договоры, документы, поиск по нормам),
затем собирает ответ из результатов — без генерации текста. Нужен для демонстрации, тестов
и как последний резерв при недоступности моделей.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import re
import uuid
from collections.abc import Callable
from typing import Any

from bis.llm.types import LLMResponse, Message, ToolCall, ToolSpec

SEARCH_TOOL = "search_knowledge"
NO_MODEL = (
    "Языковая модель не подключена (офлайн-режим). Доступны поиск по базе знаний, "
    "реестры и формирование документов по формам."
)
MONTHS = ["январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"]

Args = Callable[[str, dict[str, Any]], dict[str, Any] | None]


def _period(text: str) -> dict[str, str]:
    """«за май 2026» → период месяца; по умолчанию — текущий месяц."""
    low = text.lower()
    today = dt.date.today()
    year = int(m.group(1)) if (m := re.search(r"\b(20\d{2})\b", low)) else today.year
    month = today.month
    for i, stem in enumerate(MONTHS, 1):
        if re.search(rf"\b{stem}\w*", low) and not (stem == "ма" and not re.search(r"\bма[йяе]\b", low)):
            month = i
            break
    last = calendar.monthrange(year, month)[1]
    return {"period_start": f"{year}-{month:02d}-01", "period_end": f"{year}-{month:02d}-{last:02d}"}


def _first_id(prev: dict[str, Any], tool: str, key: str) -> str | None:
    items = (prev.get(tool) or {}).get(key) or []
    return items[0]["id"] if items else None


def _contract_id(text: str, prev: dict[str, Any]) -> str | None:
    contracts = (prev.get("find_contracts") or {}).get("contracts") or []
    for c in contracts:
        if c.get("number") and c["number"].lower() in text.lower():
            return c["id"]
    if len(contracts) == 1:
        return contracts[0]["id"]
    archive = (prev.get("find_contracts") or {}).get("archive_contracts") or []
    if not contracts and len(archive) == 1:
        return archive[0]["document_id"]
    return None


def _object(prev: dict[str, Any], text: str = "") -> str | None:
    objects = (prev.get("find_objects") or {}).get("objects") or []
    matched = [o for o in objects if (o.get("code") and o["code"].lower() in text.lower())
               or (o.get("name") and o["name"].lower() in text.lower())]
    selected = matched or objects
    return selected[0]["id"] if len(selected) == 1 else None


INTENTS: list[tuple[str, list[tuple[str, Args]]]] = [
    (r"план.?факт|отстава|\bгпр\b|график", [
        ("find_objects", lambda t, p: {}),
        ("plan_fact", lambda t, p: {"object_id": _object(p, t)} if _object(p, t) else None)]),
    (r"перерасход|м-?29|списани|расход", [
        ("find_objects", lambda t, p: {}),
        ("m29_report", lambda t, p: {"object_id": _object(p, t), **_period(t)} if _object(p, t) else None)]),
    (r"остат|баланс|материал", [
        ("find_objects", lambda t, p: {}),
        ("material_balance", lambda t, p: {"object_id": _object(p, t)} if _object(p, t) else None)]),
    (r"кс-?2|кс-?3|закрыт|закрыва", [
        ("find_contracts", lambda t, p: {}),
        ("closing_check", lambda t, p: {"contract_id": _contract_id(t, p), **_period(t)} if _contract_id(t, p) else None)]),
    (r"комплектност|проектн\w* документ", [
        ("find_objects", lambda t, p: {}),
        ("pd_completeness", lambda t, p: {"object_id": _object(p, t)} if _object(p, t) else None)]),
    (r"просроч|горит|сроки|что срочно", [
        ("list_obligations", lambda t, p: {"overdue_only": True}),
        ("list_tasks?", lambda t, p: {"overdue_only": True})]),
    (r"поручени|задач", [("list_tasks", lambda t, p: {"status": "open"})]),
    (r"договор|обязательств|неустойк", [
        ("find_contracts", lambda t, p: {}),
        ("get_contract_details", lambda t, p: {"contract_id": _contract_id(t, p)} if _contract_id(t, p) else None)]),
    (r"документ|черновик|реестр", [("list_documents", lambda t, p: {})]),
    (r"объект", [("find_objects", lambda t, p: {})]),
]


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
        if last_user < 0:
            return self._reply(NO_MODEL)
        text = messages[last_user].content
        tool_msgs = [m for m in messages[last_user + 1:] if m.role == "tool"]
        prev: dict[str, Any] = {}
        for m in tool_msgs:
            try:
                prev[m.name or ""] = json.loads(m.content)
            except json.JSONDecodeError:
                prev[m.name or ""] = {"error": m.content}
        available = {t.name for t in tools or []}
        plan = self._plan(text, available)
        for tool, args_fn in plan:
            if tool in prev:
                continue
            args = args_fn(text, prev)
            if args is None:
                contracts = (prev.get("find_contracts") or {}).get("contracts") or []
                objects = (prev.get("find_objects") or {}).get("objects") or []
                candidates = contracts if "contract" in tool or tool == "closing_check" else objects
                if len(candidates) > 1:
                    labels = [f"№ {c['number']}" if c.get("number") else c.get("name", "") for c in candidates]
                    return self._reply(NO_MODEL + "\n\nНашёл несколько вариантов: " + "; ".join(labels)
                                       + ". Какой использовать?")
                break
            call = ToolCall(id=f"off_{uuid.uuid4().hex[:8]}", name=tool, arguments=args)
            return LLMResponse(message=Message(role="assistant", tool_calls=[call]), stop_reason="tool_calls",
                               provider=self.name, model=self.model)
        if tool_msgs:
            return self._reply(self._compose(tool_msgs))
        return self._reply(NO_MODEL)

    @staticmethod
    def _plan(text: str, available: set[str]) -> list[tuple[str, Args]]:
        """Сценарий для запроса: все обязательные шаги должны быть среди инструментов агента («?» — необязательный)."""
        low = text.lower()
        for pattern, steps in INTENTS:
            if not re.search(pattern, low):
                continue
            required = [name for name, _ in steps if not name.endswith("?")]
            if all(name in available for name in required):
                return [(name.rstrip("?"), fn) for name, fn in steps if name.rstrip("?") in available]
        if SEARCH_TOOL in available:
            return [(SEARCH_TOOL, lambda t, p: {"query": t})]
        return []

    def _reply(self, text: str) -> LLMResponse:
        return LLMResponse(message=Message(role="assistant", content=text), stop_reason="end", provider=self.name,
                           model=self.model)

    def _compose(self, tool_msgs: list[Message]) -> str:
        parts = ["Офлайн-режим: ответ собран из данных системы без языковой модели."]
        for m in tool_msgs:
            if m.name in ("find_objects", "find_contracts") and len(tool_msgs) > 1:
                continue  # вспомогательные шаги
            try:
                data = json.loads(m.content)
            except json.JSONDecodeError:
                parts.append(m.content[:1500])
                continue
            parts.append(_format(m.name or "", data))
        return "\n\n".join(p for p in parts if p)


def _n(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—" if v is None else str(v)
    return f"{f:,.2f}".replace(",", " ").replace(".00", "")


def _format(tool: str, data: Any) -> str:
    if isinstance(data, dict) and data.get("error"):
        return f"Не удалось получить данные: {data['error']}"
    if tool == SEARCH_TOOL:
        results = data.get("results") or []
        if not results:
            return "В базе знаний ничего не найдено по запросу."
        lines = ["Найденные положения (проверьте применимость):"]
        for r in results[:5]:
            body = " ".join(str(r.get("content", "")).split())
            lines.append(f"[{r.get('n')}] {r.get('citation')}: {body[:500]}")
        return "\n".join(lines)
    if tool == "list_obligations":
        rows = data.get("obligations") or []
        if not rows:
            return "Просроченных обязательств нет."
        lines = [f"Обязательства по договорам ({len(rows)}):"]
        for o in rows[:15]:
            who = "мы" if o.get("party") == "we" else "контрагент"
            late = f", просрочено {-o['days_left']} дн." if (o.get("days_left") or 0) < 0 else ""
            lines.append(f"• {o.get('due_date') or o.get('due_text') or 'без срока'} — {o['description'][:160]} "
                         f"({who}{late}{', ' + o['clause'] if o.get('clause') else ''})")
        return "\n".join(lines)
    if tool == "list_tasks":
        rows = data.get("tasks") or []
        if not rows:
            return "Поручений нет."
        lines = [f"Поручения ({len(rows)}):"]
        lines += [f"• {t['title'][:160]} — срок {t.get('due_date') or '—'}, исполнитель {t.get('assignee') or 'не назначен'}"
                  for t in rows[:15]]
        return "\n".join(lines)
    if tool == "plan_fact":
        rows = data.get("rows") or []
        if not rows:
            return "График производства работ не заполнен."
        lines = [f"План-факт на {data.get('on_date')}:"]
        names = {"done": "исполнено", "behind": "отставание", "overdue": "просрочено", "on_track": "по графику",
                 "ahead": "опережение", "not_started": "не начато", "no_data": "нет данных"}
        for r in rows:
            fc = f", прогноз окончания {r['forecast_end']}" if r.get("forecast_end") else ""
            lines.append(f"• {r['name'][:90]}: факт {_n(r['actual'])} из {_n(r['planned_quantity'])} {r.get('unit') or ''} "
                         f"({r['percent_fact']}%) — {names.get(r['status'], r['status'])}{fc}")
        return "\n".join(lines)
    if tool == "material_balance":
        rows = data.get("materials") or []
        lines = ["Остатки материалов на объекте:"]
        for b in rows:
            warn = f" ⚠ {b['problem']}" if b.get("problem") else ""
            lines.append(f"• {b['material']}: приход {_n(b['received'])}, расход {_n(b['used'])}, "
                         f"остаток {_n(b['balance'])} {b['unit']}{warn}")
        return "\n".join(lines) if rows else "Движения материалов нет."
    if tool == "m29_report":
        rows = data.get("rows") or []
        if not rows:
            return "За период нет подтверждённых объёмов и расхода материалов."
        names = {"ok": "в норме", "economy": "экономия", "overspend": "ПЕРЕРАСХОД", "no_actual": "нет факта", "no_norm": "нет нормы"}
        lines = [f"Расход материалов за {data['period'][0]} — {data['period'][1]} (М-29):"]
        for r in rows:
            dev = f", отклонение {_n(r['deviation'])} ({r['deviation_percent']}%)" if r.get("deviation") is not None else ""
            lines.append(f"• {r['material']}: по норме {_n(r['norm_total'])}, факт {_n(r['actual'])} {r['unit']}{dev} — "
                         f"{names.get(r['status'], r['status'])}")
        return "\n".join(lines)
    if tool == "closing_check":
        rows = data.get("rows") or []
        lines = [f"Можно закрыть в КС-2 за {data['period'][0]} — {data['period'][1]}:"]
        lines += [f"• {r.get('code') or ''} {r['name'][:90]}: {_n(r['include_quantity'])} {r['unit']} × "
                  f"{_n(r['unit_price'])} = {_n(r['amount'])} руб." for r in rows] or ["• нечего закрывать"]
        if rows:
            lines.append(f"Итого {_n(data['total'])} руб., НДС {_n(data['vat'])}, всего {_n(data['total_with_vat'])} руб.")
        for i in data.get("issues") or []:
            lines.append(f"⚠ {i['message']}")
        return "\n".join(lines)
    if tool == "get_contract_details":
        obligations = data.get("obligations") or []
        a = data.get("analysis") or {}
        lines = [f"Договор № {data.get('number')} от {data.get('date')} с {data.get('counterparty')}: "
                 f"{_n(data.get('amount'))} руб., окончание {data.get('end_date')}."]
        if a.get("risks"):
            lines.append("Риски: " + "; ".join(a["risks"][:5]))
        lines.append(f"Обязательства ({len(obligations)}):")
        lines += [f"• {o.get('due_date') or o.get('due_text') or 'без срока'} — {o['description'][:140]} [{o['status']}]"
                  for o in obligations[:12]]
        return "\n".join(lines)
    if tool == "list_documents":
        rows = data.get("documents") or []
        return "\n".join([f"Документы ({len(rows)}):"] + [f"• {d['title'][:140]} — {d['status']}" for d in rows[:15]])
    if tool == "find_objects":
        rows = data.get("objects") or []
        return "\n".join(["Объекты:"] + [f"• {o.get('code') or ''} {o['name']}" for o in rows])
    if tool == "find_contracts":
        rows = data.get("contracts") or []
        return "\n".join(["Договоры:"] + [f"• № {c['number']} — {c.get('counterparty') or ''}: {(c.get('subject') or '')[:100]}"
                                           for c in rows])
    if tool == "pd_completeness":
        missing = data.get("missing_required") or []
        return ("Комплектность ПД: " + ("все обязательные разделы на месте." if not missing else
                                        "нет разделов — " + "; ".join(missing)))
    return json.dumps(data, ensure_ascii=False, default=str)[:1500]


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
