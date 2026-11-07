"""ИИ-специалисты: каждый силён в своём и работает на своей модели (маршрут LLM-шлюза).

Специалисты восприятия (зрение, чтение документов, слух) превращают сырые сигналы с производства в данные;
инженерные специалисты (ПТО, юрист, экономист, геодезист) делают работу; контролёр сверяет источники;
аналитик прогнозирует. Диспетчер решает, кому передать входящее.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bis import voice
from bis.llm.gateway import LLMGateway
from bis.models import AIAction


@dataclass(frozen=True)
class Specialist:
    name: str
    title: str
    strength: str
    capability: str  # text | vision | speech | rules
    task: str  # маршрут LLM-шлюза
    agent: str | None = None  # агент для диалога, если есть


SPECIALISTS: list[Specialist] = [
    Specialist("dispatcher", "Диспетчер", "Понимает, что пришло с объекта, и передаёт нужному специалисту", "text", "route"),
    Specialist("vision", "Зрение", "Фото с площадки: вид и стадия работ, дефекты, нарушения охраны труда; фото документов",
               "vision", "vision"),
    Specialist("reader", "Чтец документов", "УПД, ТТН, сертификаты, паспорта качества, протоколы испытаний → данные",
               "text", "extract"),
    Specialist("hearing", "Слух", "Голосовые сообщения прорабов → объёмы работ, расход материалов, проблемы", "speech", "extract"),
    Specialist("pto", "Инженер ПТО", "Сам собирает АОСР и АООК из фактов: участники, материалы с сертификатами, схемы, нормы",
               "text", "chat", agent="pto"),
    Specialist("lawyer", "Юрист", "Договоры: обязательства, сроки, неустойки, риски, претензии", "text", "analysis",
               agent="contracts"),
    Specialist("economist", "Экономист", "КС-2/КС-3, М-29, сверка проекта со сметой, перерасход", "text", "chat", agent="closing"),
    Specialist("surveyor", "Геодезист", "Исполнительные схемы по съёмке: отклонения, допуски, DXF", "rules", "chat", agent="cad"),
    Specialist("secretary", "Секретарь", "Письма: регистрация, суть, сроки ответа, поручения", "text", "extract", agent="secretary"),
    Specialist("verifier", "Контролёр", "Сверяет источники и ловит противоречия: расход без прихода, цена выше обычной, "
               "объём сверх сметы, материалы без сертификатов", "rules", "verify"),
    Specialist("analyst", "Аналитик", "Прогноз сроков и отставаний по фактическому темпу компании", "rules", "analysis",
               agent="works"),
]
BY_NAME = {s.name: s for s in SPECIALISTS}


def model_status(gateway: LLMGateway, s: Specialist) -> dict[str, str | bool]:
    """Какая модель обслуживает специалиста, или он работает на правилах."""
    if s.capability == "speech":
        from bis.config import get_settings

        on = voice.status()["stt"]
        engine = "Yandex SpeechKit" if get_settings().stt_provider == "yandex" else "Whisper"
        return {"connected": on, "model": f"распознавание речи ({engine})" if on else "не подключено — нужен сервер STT"}
    if s.capability == "rules":
        llm = gateway.has_capability("text", s.task)
        return {"connected": True, "model": "алгоритмы" + (f" + {llm[0]}" if llm else "")}
    providers = gateway.has_capability(s.capability, s.task)
    if s.name == "reader":
        from bis import ocr

        if ocr.available():
            base = f"{providers[0]} ({getattr(gateway.entries[providers[0]].provider, 'model', '')})" if providers else "правила"
            return {"connected": True, "model": f"{base} + Yandex Vision OCR (сканы и фото документов)"}
    if providers:
        entry = gateway.entries[providers[0]]
        return {"connected": True, "model": f"{providers[0]} ({getattr(entry.provider, 'model', '')})"}
    hint = "нужна модель зрения" if s.capability == "vision" else "работает на правилах, нужна языковая модель"
    return {"connected": False, "model": f"не подключена — {hint}"}


def stats(db: Session, org_id) -> dict[str, dict[str, int | float | None]]:
    """Сколько специалист сделал сам, сколько утвердили, исправили и отклонили люди; точность — доля принятых без правок."""
    rows = db.execute(select(AIAction.specialist, AIAction.status, AIAction.auto, AIAction.corrected,
                             AIAction.reviewed_at.is_not(None), func.count())
                      .where(AIAction.organization_id == org_id)
                      .group_by(AIAction.specialist, AIAction.status, AIAction.auto, AIAction.corrected,
                                AIAction.reviewed_at.is_not(None))).all()
    out: dict[str, dict[str, int | float | None]] = {}
    for spec, status, auto, corrected, reviewed, n in rows:
        st = out.setdefault(spec, {"total": 0, "auto": 0, "approved": 0, "corrected": 0, "rejected": 0, "pending": 0,
                                   "failed": 0})
        st["total"] += n
        if status == "rejected":
            st["rejected"] += n
        elif status == "proposed":
            st["pending"] += n
        elif status == "failed":
            st["failed"] += n
        elif corrected:
            st["corrected"] += n
        elif reviewed:
            st["approved"] += n
        elif auto:
            st["auto"] += n
    for st in out.values():
        decided = st["approved"] + st["corrected"] + st["rejected"]
        st["accuracy"] = round(st["approved"] / decided * 100, 1) if decided else None
    return out
