"""Самопроверка «Эрудита»: вопросы по своему же архиву → ответы ассистента → процент верных.

Вопросы составляются по карточкам документов, а правильный ответ берётся из карточки и обязательно сверяется
с текстом самого документа (иначе вопрос не задаётся). Документ в вопросе называется так, как его назвал бы
человек: вид, номер, дата. Проверяется:
- нашёл ли ассистент нужный документ (он в источниках или под ответом);
- есть ли в ответе правильное значение (шифр, дата, номер, оси).
Свои вопросы можно добавить файлом: «вопрос → ответ», по одному на строку.
"""

from __future__ import annotations

import datetime as dt
import random
import re
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.brain.card import KIND_TITLES
from bis.models import Document, DocumentLink

_LATIN = str.maketrans("ABCEHKMOPTXYabcehkmoptxy", "АВСЕНКМОРТХУавсенкмортху")


def norm(s: str) -> str:
    """Для сравнения: регистр, ё, латиница/кириллица (B25 = В25), пробелы и тире."""
    s = s.lower().replace("ё", "е").translate(_LATIN)
    return re.sub(r"[\s\-–—]+", "", s)


def _ru(iso: str | None) -> str | None:
    try:
        return f"{dt.date.fromisoformat(iso):%d.%m.%Y}" if iso else None
    except ValueError:
        return None


def _name(doc: Document, card: dict) -> str:
    """Как назвал бы документ человек: «АОСР № 2 от 14.04.2026»."""
    head = KIND_TITLES.get(card.get("kind") or doc.kind, "документ")
    num = card.get("number") or doc.number
    date = _ru(card.get("date")) or (f"{doc.date:%d.%m.%Y}" if doc.date else None)
    return head + (f" № {num}" if num else "") + (f" от {date}" if date else "")


def _in_text(text: str, value: str) -> bool:
    return bool(value) and norm(value) in norm(text)


def generate(db: Session, org_id: uuid.UUID, n: int = 40, seed: int = 7, per_doc: int = 3) -> list[dict[str, Any]]:
    from bis.brain.build import document_text

    cases: list[dict[str, Any]] = []
    docs = db.scalars(select(Document).where(Document.organization_id == org_id, Document.data["card"].is_not(None))
                      .order_by(Document.created_at)).all()
    for d in docs:
        c = (d.data or {}).get("card") or {}
        if not (c.get("number") or d.number):
            continue  # без номера документ в вопросе не назвать однозначно
        name, kind = _name(d, c), c.get("kind") or d.kind
        text = document_text(db, d)
        if not text.strip():
            continue

        def add(question: str, expect: list[str], what: str, text: str = text, d: Document = d) -> None:
            expect = [e for e in expect if e]
            if expect and all(_in_text(text, e) for e in expect):  # эталон — только то, что есть в тексте
                cases.append({"question": question, "expect": expect, "document_id": str(d.id), "what": what,
                              "source": "auto"})

        cases.append({"question": f"Найди {name}", "expect": [], "document_id": str(d.id), "what": "поиск документа",
                      "source": "auto"})
        if c.get("rd_code"):
            add(f"Какой шифр рабочей документации указан в {name}?", [c["rd_code"]], "шифр РД")
        if kind in ("aosr", "aook"):
            if c.get("work_end"):
                add(f"Когда окончены работы по {name}?", [_ru(c["work_end"])], "дата окончания работ")
            if c.get("axes"):
                add(f"В каких осях выполнены работы по {name}?", [c["axes"][0]], "оси")
            qual = [r["number"] for r in c.get("refs") or [] if r.get("kind") == "quality"]
            if qual:
                add(f"Какие документы о качестве указаны в {name}?", qual[:2], "документы о качестве")
        if kind in ("letter", "incoming_letter") and c.get("deadline"):
            add(f"К какому сроку просят ответить или выполнить в письме {name}?", [_ru(c["deadline"])], "срок письма")
        if kind == "incoming_letter":
            reply = db.scalar(select(Document).join(DocumentLink, DocumentLink.from_document_id == Document.id)
                              .where(DocumentLink.to_document_id == d.id, DocumentLink.relation == "letter",
                                     Document.kind == "letter"))
            if reply is not None and (reply.number or ((reply.data or {}).get("card") or {}).get("number")):
                num = reply.number or reply.data["card"]["number"]
                cases.append({"question": f"Ответили ли мы на {name}? Каким письмом?", "expect": [num],
                              "document_id": str(reply.id), "what": "ответ на письмо", "source": "auto"})
        if kind in ("certificate", "test_protocol") and c.get("grades"):
            add(f"На какую продукцию и какой марки {name}?", [c["grades"][0]], "марка в документе о качестве")
    random.Random(seed).shuffle(cases)
    # не больше трёх вопросов на документ — чтобы проверка была шире, а не глубже по одному
    per: dict[str, int] = {}
    out = []
    for case in cases:
        if per.get(case["document_id"], 0) < per_doc:
            out.append(case)
            per[case["document_id"]] = per.get(case["document_id"], 0) + 1
        if len(out) >= n:
            break
    return out


def parse_questions(text: str) -> list[dict[str, Any]]:
    """Свои вопросы: «вопрос → ответ» (ответ необязателен). Проверяются номера, коды, даты и марки из ответа."""
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("-•0123456789.) ").strip()
        if not line or line.startswith("#"):
            continue
        q, _, a = line.partition("→") if "→" in line else line.partition("->")
        tokens = re.findall(r"[A-Za-zА-Яа-яЁё]*\d[\w.\-/]*|[A-ZА-Я]{2,}\d*", a)
        out.append({"question": q.strip(), "expect": tokens or ([a.strip()] if a.strip() else []), "document_id": None,
                    "what": "ваш вопрос", "source": "user", "answer": a.strip() or None})
    return out


def run(db: Session, user, cases: list[dict[str, Any]], progress=None) -> dict[str, Any]:
    from bis.api.deps import agent_context
    from bis.llm.gateway import get_gateway
    from bis.plugins.sdk import get_registry

    reg = get_registry()
    results = []
    for i, case in enumerate(cases, 1):
        t = time.perf_counter()
        try:
            name, _ = reg.supervisor().route(case["question"], get_gateway())
            ctx = agent_context(db, user)
            res = reg.agents[name].run(ctx, reg.tools, [], case["question"])
            text = res.text
            found_ids = {d.get("id") for d in res.documents} | {c.get("document_id") for c in res.citations}
            error = None
        except Exception as e:  # noqa: BLE001 — один вопрос не останавливает проверку
            name, text, found_ids, error = "—", "", set(), str(e)[:300]
        finally:
            db.rollback()  # проверка ничего не меняет в данных
        found = case["document_id"] in found_ids if case.get("document_id") else None
        correct = all(norm(e) in norm(text) for e in case["expect"]) if case["expect"] else None
        results.append({**case, "agent": name, "seconds": round(time.perf_counter() - t, 1), "found": found,
                        "correct": correct, "answer_text": text[:600], "error": error})
        if progress:
            progress(i, len(cases))
    graded = [r for r in results if r["correct"] is not None]
    searched = [r for r in results if r["found"] is not None]
    summary = {
        "questions": len(results),
        "correct": sum(1 for r in graded if r["correct"]), "graded": len(graded),
        "accuracy": round(100 * sum(1 for r in graded if r["correct"]) / len(graded)) if graded else None,
        "found": sum(1 for r in searched if r["found"]), "searched": len(searched),
        "found_rate": round(100 * sum(1 for r in searched if r["found"]) / len(searched)) if searched else None,
        "avg_seconds": round(sum(r["seconds"] for r in results) / len(results), 1) if results else None,
        "errors": sum(1 for r in results if r["error"]),
        "by_what": {},
    }
    for r in graded:
        b = summary["by_what"].setdefault(r["what"], {"ok": 0, "total": 0})
        b["total"] += 1
        b["ok"] += 1 if r["correct"] else 0
    return {"at": dt.datetime.now().isoformat(timespec="seconds"), "summary": summary, "results": results}
