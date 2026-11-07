"""Оценка качества на наборе вопросов организации — основа контролируемого «доучивания».

Набор (JSON): [{"question": "...", "expect_citation": "СП 70.13330.2012, п. 5.16", "expect_agent": "norms",
               "expect_contains": ["0,3 м"]}]
- Поиск: попала ли ожидаемая ссылка в top-k (hit@k) и на какой позиции (MRR) — без языковой модели.
- Маршрутизация: какой агент выбран.
- Ответы (по флагу): содержит ли ответ агента ожидаемые фрагменты — нужна подключённая модель.
Запускайте после загрузки документов, смены модели или эмбеддингов: метрики не должны падать.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from bis.rag.embeddings import Embedder
from bis.rag.search import hybrid_search


class EvalCase(BaseModel):
    question: str
    expect_citation: str | None = None
    expect_agent: str | None = None
    expect_contains: list[str] = Field(default_factory=list)
    scope: str = "all"


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("ё", "е").split())


def run_eval(db: Session, embedder: Embedder, org_id: uuid.UUID, cases: list[EvalCase], *, k: int = 5,
             answer_fn=None, route_fn=None) -> dict[str, Any]:
    from bis.agents.builtin_tools import SCOPE_KINDS

    results = []
    hits = rr = routed_ok = routed_total = answers_ok = answers_total = 0
    retrieval_total = 0
    for case in cases:
        row: dict[str, Any] = {"question": case.question}
        if case.expect_citation:
            retrieval_total += 1
            found = hybrid_search(db, embedder, case.question, org_id=org_id, kinds=SCOPE_KINDS.get(case.scope), k=k)
            target = _norm(case.expect_citation)
            rank = next((i for i, h in enumerate(found, 1) if target in _norm(h.citation)), None)
            row.update(rank=rank, top=[h.citation for h in found[:3]])
            if rank:
                hits += 1
                rr += 1 / rank
        if case.expect_agent and route_fn:
            routed_total += 1
            agent = route_fn(case.question)
            row["agent"] = agent
            routed_ok += agent == case.expect_agent
        if case.expect_contains and answer_fn:
            answers_total += 1
            answer = answer_fn(case.question)
            ok = all(_norm(x) in _norm(answer) for x in case.expect_contains)
            row.update(answer=answer[:500], answer_ok=ok)
            answers_ok += ok
        results.append(row)
    return {
        "cases": len(cases),
        f"hit@{k}": round(hits / retrieval_total, 3) if retrieval_total else None,
        "mrr": round(rr / retrieval_total, 3) if retrieval_total else None,
        "routing_accuracy": round(routed_ok / routed_total, 3) if routed_total else None,
        "answer_accuracy": round(answers_ok / answers_total, 3) if answers_total else None,
        "results": results,
    }
