"""ИИ-конвейер производства.

Сигнал с объекта (фото, голосовое, документ, текст) → восприятие (слух, зрение, чтение) → диспетчер определяет,
что это → специалист извлекает данные и предлагает действия → политика самостоятельности решает, выполнить сразу
или отдать человеку → контролёр сверяет с другими источниками → инженер ПТО собирает документы из накопленных фактов.

Человек работает с результатом («Лента ИИ»): утверждает, исправляет или отклоняет. Исправление становится примером
для сопоставления (материал, позиция сметы), причина отклонения — правилом для специалиста.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import mimetypes
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from bis import voice
from bis.ai import understanding as und
from bis.ai.specialists import BY_NAME
from bis.llm.gateway import LLMGateway
from bis.models import (
    AIAction,
    Appointment,
    ConstructionObject,
    Contract,
    Counterparty,
    Document,
    EstimateItem,
    IntakeEvent,
    KnowledgeSource,
    Material,
    MaterialNorm,
    MaterialReceipt,
    MaterialUsage,
    Organization,
    OrgMemory,
    Person,
    ScheduleTask,
    Task,
    User,
    WorkVolume,
)
from bis.rag.embeddings import Embedder
from bis.rag.parsers import UnsupportedFormat, parse_file
from bis.services import works as wsvc
from bis.services.audit import audit
from bis.services.notify import managers, notify
from bis.services.storage import Storage

log = logging.getLogger(__name__)

# Что ИИ делает сам, а что предлагает человеку (переопределяется в settings.autonomy организации)
DEFAULT_AUTONOMY: dict[str, str] = {
    "register_document": "auto", "report_volume": "auto", "record_usage": "auto", "link_certificate": "auto",
    "create_task": "auto", "flag_issue": "auto", "info": "auto", "process_letter": "auto", "build_scheme": "auto",
    "draft_aosr": "auto", "create_receipt": "propose", "import_contract": "propose",
}
AUTO_MIN_CONFIDENCE = 0.7
ACTION_TITLES = {
    "register_document": "Регистрация документа", "report_volume": "Выполненный объём", "record_usage": "Расход материала",
    "link_certificate": "Документ о качестве", "create_task": "Поручение", "flag_issue": "Замечание",
    "info": "Сведения", "process_letter": "Разбор письма", "build_scheme": "Исполнительная схема",
    "draft_aosr": "Черновик АОСР", "create_receipt": "Приход материала", "import_contract": "Импорт договора",
}
# Что человек может исправить перед утверждением
EDITABLE: dict[str, tuple[str, ...]] = {
    "create_receipt": ("material_id", "quantity", "price", "object_id", "date", "quality_doc"),
    "report_volume": ("estimate_item_id", "quantity", "location", "object_id", "date"),
    "record_usage": ("material_id", "quantity", "object_id", "date"),
    "create_task": ("title", "role", "due_days", "priority"),
    "draft_aosr": ("works", "project_docs", "materials", "executive_docs", "norms", "next_works"),
    "import_contract": ("kind", "our_role", "object_id"),
    "link_certificate": ("receipt_ids", "text"),
}
NEEDS_OBJECT = ("create_receipt", "report_volume", "record_usage", "draft_aosr")
DOC_KIND = {"upd": "supply_doc", "certificate": "certificate", "test_protocol": "test_protocol", "letter": "incoming_letter",
            "contract": "contract", "project_doc": "project_doc", "survey": "survey", "site_photo": "photo", "unknown": "other"}
KNOWLEDGE_KIND = {"letter": "letter", "contract": "contract", "project_doc": "project_doc"}
COMPLETE_RE = re.compile(r"закончил|заверш|готов\w* к (?:при[её]мк|освидетельств|сдач)|предъяв|можно принимать|"
                         r"выполнен\w* полностью|сда[её]м", re.IGNORECASE)


@dataclass
class Env:
    db: Session
    storage: Storage
    gateway: LLMGateway | None
    embedder: Embedder
    org_id: uuid.UUID
    user: User | None = None
    created: list[AIAction] = field(default_factory=list)
    _autonomy: dict[str, str] | None = None

    def autonomy(self) -> dict[str, str]:
        if self._autonomy is None:
            org = self.db.get(Organization, self.org_id)
            self._autonomy = {**DEFAULT_AUTONOMY, **((org.settings or {}).get("autonomy") or {})}
        return self._autonomy


# --- приём -----------------------------------------------------------------------------------------


def detect_kind(file_name: str | None, mime: str | None) -> str:
    mime = mime or mimetypes.guess_type(file_name or "")[0] or ""
    ext = Path(file_name or "").suffix.lower()
    if mime.startswith("image/") or ext in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
        return "photo"
    if mime.startswith("audio/") or ext in (".ogg", ".oga", ".mp3", ".wav", ".m4a", ".webm", ".opus"):
        return "voice"
    return "document"


def ingest(env: Env, *, source: str, text: str | None = None, data: bytes | None = None, file_name: str | None = None,
           mime: str | None = None, object_id: uuid.UUID | None = None, sender: str | None = None) -> IntakeEvent:
    """Сохраняет сигнал с производства. Обработка — process()."""
    key = None
    if data is not None:
        file_name = file_name or "file"
        key = env.storage.put(data, file_name, prefix=f"intake/{env.org_id}")
    event = IntakeEvent(organization_id=env.org_id, created_at=now(), source=source,
                        kind=detect_kind(file_name, mime) if key else "text",
                        text=(text or "").strip() or None, file_key=key, file_name=file_name,
                        mime=mime or (mimetypes.guess_type(file_name)[0] if file_name else None), object_id=object_id,
                        user_id=env.user.id if env.user else None,
                        sender=sender or (env.user.full_name if env.user else None), status="new", result={})
    env.db.add(event)
    env.db.flush()
    return event


def process(env: Env, event: IntakeEvent) -> list[AIAction]:
    env.created = []
    try:
        with env.db.begin_nested():
            _process(env, event)
        event.status = "done"
    except Exception as e:  # noqa: BLE001 — сбой одного сигнала не должен останавливать конвейер
        log.exception("Конвейер: событие %s", event.id)
        env.created = []
        event.status, event.error = "error", str(e)[:2000]
        event.result = {**(event.result or {}), "reply": reply_text(event, [])}
    env.db.flush()
    return env.created


def _process(env: Env, event: IntakeEvent) -> None:
    text = event.text or ""
    images: list[dict[str, str]] | None = None
    data = env.storage.get(event.file_key) if event.file_key else None

    # 1. восприятие
    if event.kind == "voice":
        if not voice.status()["stt"]:
            _act(env, event, "hearing", "flag_issue", "Голосовое сохранено, но не распознано",
                 "Распознавание речи не подключено (сервер STT). Подключите — и голосовые прорабов станут объёмами, "
                 "расходом и поручениями.", {"audience": "admins"}, 1.0, severity="warning")
            return _finish(env, event)
        try:
            transcript = voice.transcribe(data, event.file_name or "voice.ogg", event.mime)
        except voice.VoiceError as e:
            _act(env, event, "hearing", "flag_issue", "Голосовое не распознано", f"{e}. Сообщение сохранено.",
                 {"audience": "admins"}, 1.0, severity="warning")
            return _finish(env, event)
        if not transcript:
            _act(env, event, "hearing", "info", "В голосовом не найдено речи", "Запись сохранена.", {}, 1.0)
            event.result = {**event.result, "transcript": ""}
            return _finish(env, event)
        text = (transcript + (f"\n{event.text}" if event.text else "")).strip()
        event.result = {**event.result, "transcript": transcript}
    elif event.kind == "photo":
        images = [{"mime": event.mime or "image/jpeg", "data": base64.b64encode(data).decode()}]
        doc_text = _ocr(event, data)
        if doc_text and len(doc_text) >= 60:
            dtype, conf, by = und.classify(env.gateway, doc_text, None, "document")
            if dtype not in ("unknown", "site_photo", "foreman_report"):  # на фото — документ: читаем его текст
                text = (doc_text + ("\n" + event.text if event.text else "")).strip()
                event.result = {**event.result, "photo_of_document": True}
                event.object_id = event.object_id or resolve_object(env, text)
                # текст уже прочитан OCR — дальше работает языковая модель без изображения
                return _dispatch(env, event, text, None, (dtype, conf, f"{by}+ocr"))
    elif event.kind == "document":
        try:
            parsed = parse_file(data, event.file_name or "file", event.mime)
            text = (parsed.text + "\n" + (event.text or "")).strip()
            if parsed.meta.get("needs_ocr") or not parsed.text.strip():
                scanned = _ocr(event, data)
                if scanned:
                    text = (scanned + "\n" + (event.text or "")).strip()
                else:
                    event.result = {**event.result, "needs_ocr": True}
        except UnsupportedFormat as e:
            event.result = {**event.result, "unsupported": str(e)}
    if event.object_id is None:
        event.object_id = resolve_object(env, text)

    # 2. диспетчер
    if event.kind == "photo":
        found = _photo_type(env, event, images, text)
    else:
        found = und.classify(env.gateway, text, event.file_name, event.kind)
    _dispatch(env, event, text, images, found)


def _ocr(event: IntakeEvent, data: bytes) -> str:
    """Скан или фото документа → текст (если подключено распознавание)."""
    from bis import ocr

    if not ocr.available():
        return ""
    try:
        text = ocr.recognize(data, event.mime or mimetypes.guess_type(event.file_name or "")[0])
    except ocr.OCRError as e:
        event.result = {**event.result, "ocr_error": str(e)[:300]}
        return ""
    if text:
        event.result = {**event.result, "ocr": True}
    return text


def _dispatch(env: Env, event: IntakeEvent, text: str, images, found: tuple[str, float, str]) -> None:
    dtype, conf, by = found
    event.detected_type = dtype
    event.result = {**event.result, "dispatcher": {"type": dtype, "title": und.TYPES[dtype], "confidence": conf, "by": by},
                    "excerpt": text[:600]}

    # 3. специалист
    PIPELINES[dtype](env, event, text, images)
    _finish(env, event)


def _finish(env: Env, event: IntakeEvent) -> None:
    _decide_all(env)  # 4. политика самостоятельности
    verify(env, event, [a for a in env.created if a.status == "applied"])  # 5. контролёр и инженер ПТО
    _decide_all(env)
    event.result = {**event.result, "reply": reply_text(event, env.created)}


def _decide_all(env: Env) -> None:
    for a in list(env.created):
        if a.status == "new":
            _decide(env, a)


def _decide(env: Env, a: AIAction) -> None:
    mode = env.autonomy().get(a.kind, "propose")
    if a.kind in NEEDS_OBJECT and not a.payload.get("object_id"):
        a.status = "proposed"
        a.summary = ((a.summary or "") + " Укажите объект — сообщение не удалось привязать к объекту.").strip()
    elif mode == "auto" and a.confidence >= AUTO_MIN_CONFIDENCE:
        apply_action(env, a, auto=True)
    else:
        a.status = "proposed"


def reply_text(event: IntakeEvent, actions: list[AIAction]) -> str:
    """Короткий ответ отправителю: что ИИ понял и сделал."""
    d = (event.result or {}).get("dispatcher") or {}
    lines = [f"Понял: {d.get('title', 'сообщение').lower()}."]
    icon = {"applied": "✓", "proposed": "?", "failed": "✗"}
    for a in actions:
        if a.kind == "register_document":
            continue
        mark = "⚠" if a.kind == "flag_issue" and a.severity != "info" else icon.get(a.status, "·")
        lines.append(f"{mark} {a.title}" + (" — ждёт подтверждения" if a.status == "proposed" else ""))
    if event.status == "error":
        lines.append("Не удалось обработать — сообщение сохранено, специалисты посмотрят.")
    return "\n".join(lines[:15])


def now() -> dt.datetime:
    """Время с микросекундами: порядок событий и действий внутри одной транзакции сохраняется."""
    return dt.datetime.now(dt.UTC)


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n100, n10 = abs(n) % 100, abs(n) % 10
    return forms[2] if 11 <= n100 <= 14 else forms[0] if n10 == 1 else forms[1] if 2 <= n10 <= 4 else forms[2]


def fmt(v: Any) -> str:
    """Число без лишних нулей: 52, 5.5, 0.125."""
    return f"{float(v):.3f}".rstrip("0").rstrip(".")


# --- привязка к объекту ----------------------------------------------------------------------------

GENERIC_NAME_STEMS = wsvc._stems("жилой дом корпус строительство объект комплекс здание секция этап очередь улица")


def resolve_object(env: Env, text: str = "") -> uuid.UUID | None:
    """Объект по тексту (шифр, название), по назначению отправителя приказом или единственный активный."""
    objs = env.db.scalars(select(ConstructionObject).where(ConstructionObject.organization_id == env.org_id,
                                                           ConstructionObject.status == "active")).all()
    low = (text or "").lower()
    if low:
        by_code = [o for o in objs if o.code and re.search(rf"(?<!\w){re.escape(o.code.lower())}(?!\w)", low)]
        if len(by_code) == 1:
            return by_code[0].id
        stems = wsvc._stems(text[:1500])
        named = [o for o in objs if (own := wsvc._stems(o.name) - GENERIC_NAME_STEMS) and len(own & stems) / len(own) >= 0.6]
        if len(named) == 1:
            return named[0].id
    if env.user is not None:
        person = env.db.scalar(select(Person).where(Person.organization_id == env.org_id, Person.user_id == env.user.id))
        if person:
            mine = {a.object_id for a in env.db.scalars(select(Appointment).where(Appointment.person_id == person.id))}
            mine &= {o.id for o in objs}
            if len(mine) == 1:
                return mine.pop()
    return objs[0].id if len(objs) == 1 else None


# --- память: примеры от людей и правила ------------------------------------------------------------


def _aliases(env: Env, what: str) -> list[tuple[str, uuid.UUID]]:
    rows = env.db.scalars(select(OrgMemory).where(OrgMemory.organization_id == env.org_id, OrgMemory.kind == "alias",
                                                  OrgMemory.scope == f"alias:{what}", OrgMemory.is_active.is_(True))
                          .order_by(OrgMemory.created_at.desc()).limit(1000)).all()
    out = []
    for m in rows:
        try:
            out.append((m.content, uuid.UUID(m.source or "")))
        except ValueError:
            continue
    return out


def remember_alias(env: Env, what: str, text: str, target_id: Any) -> None:
    """Запоминает: «так у нас называют» → материал / позиция сметы (в source — id цели)."""
    text = (text or "").strip()[:2000]
    if not text or not target_id:
        return
    row = env.db.scalar(select(OrgMemory).where(OrgMemory.organization_id == env.org_id, OrgMemory.kind == "alias",
                                                OrgMemory.scope == f"alias:{what}", OrgMemory.content == text))
    if row:
        row.source, row.is_active = str(target_id), True
    else:
        env.db.add(OrgMemory(organization_id=env.org_id, kind="alias", scope=f"alias:{what}", content=text,
                             source=str(target_id)))
    env.db.flush()


def hints(env: Env, specialist: str) -> str:
    """Правила и замечания организации для специалиста — добавляются к инструкциям модели."""
    spec = BY_NAME.get(specialist)
    scopes = ["*", specialist, "conveyor"] + ([spec.agent] if spec and spec.agent else [])
    rows = env.db.scalars(select(OrgMemory).where(OrgMemory.organization_id == env.org_id, OrgMemory.is_active.is_(True),
                                                  OrgMemory.kind.in_(["rule", "feedback"]), OrgMemory.scope.in_(scopes))
                          .order_by(OrgMemory.created_at.desc()).limit(20)).all()
    if not rows:
        return ""
    return "\n\nПравила и замечания организации (учитывай):\n" + "\n".join(f"- {m.content}" for m in rows)


def match_material(env: Env, name: str, unit: str | None,
                   prefer: set[uuid.UUID] | None = None) -> tuple[Material | None, float]:
    """prefer — материалы по нормам работ из того же сообщения: «бетона ушло» — это бетон той работы, что залили."""
    mats = {m.id: m for m in env.db.scalars(select(Material).where(Material.organization_id == env.org_id))}
    bonus = lambda m: 0.15 if prefer and m.id in prefer else 0.0  # noqa: E731

    def ok_unit(m: Material) -> bool:
        return not unit or und.norm_unit(m.unit) == und.norm_unit(unit)

    best, score = None, 0.0
    for text, target in _aliases(env, "materials"):
        m = mats.get(target)
        if m is None or not ok_unit(m):
            continue
        s = 1.0 if text.lower() == name.strip().lower() else min(1.0, wsvc.similarity(name, text) + 0.1)
        if s > score:
            best, score = m, s
    for m in mats.values():
        if ok_unit(m):
            s = wsvc.similarity(name, m.name)
            s = min(1.0, s + bonus(m)) if s > 0 else s
            if s > score:
                best, score = m, s
    return (best, score) if score >= 0.3 else (None, score)


def match_work(env: Env, description: str, unit: str | None,
               object_id: uuid.UUID | None) -> tuple[EstimateItem | None, float]:
    def ok_unit(it: EstimateItem) -> bool:
        return not unit or und.norm_unit(it.unit) == und.norm_unit(unit)

    found = wsvc.find_items(env.db, env.org_id, description, object_id=object_id, limit=8)
    best: tuple[EstimateItem | None, float] = max(((it, s) for it, s in found if ok_unit(it)), key=lambda x: x[1],
                                                  default=(None, 0.0))
    for text, target in _aliases(env, "works"):
        it = env.db.get(EstimateItem, target)
        if it is None or it.organization_id != env.org_id or (object_id and it.object_id != object_id) or not ok_unit(it):
            continue
        s = min(1.0, wsvc.similarity(description, text) + 0.1)  # пример от человека весит больше
        if s > best[1]:
            best = (it, s)
    return best


# --- действия --------------------------------------------------------------------------------------


def _uuid(v: Any) -> uuid.UUID | None:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v)) if v else None


def _json(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _json(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_json(x) for x in v]
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dt.date | uuid.UUID):
        return str(v)
    return v


def _act(env: Env, event: IntakeEvent | None, specialist: str, kind: str, title: str, summary: str,
         payload: dict[str, Any], confidence: float, *, severity: str = "info",
         evidence: list[dict[str, Any]] | None = None) -> AIAction:
    obj = payload.get("object_id") or (event.object_id if event else None)
    a = AIAction(organization_id=env.org_id, created_at=now(), event_id=event.id if event else None, specialist=specialist,
                 kind=kind,
                 title=title[:500], summary=summary, payload=_json(payload), confidence=round(min(max(confidence, 0), 1), 2),
                 severity=severity, status="new", evidence=_json(evidence or []), result={}, object_id=_uuid(obj))
    env.db.add(a)
    env.db.flush()
    env.created.append(a)
    return a


def _file_evidence(event: IntakeEvent | None) -> list[dict[str, Any]]:
    if event is None:
        return []
    if event.file_key:
        return [{"type": "file", "event_id": str(event.id), "label": event.file_name or "файл"}]
    return [{"type": "message", "event_id": str(event.id), "label": (event.text or "")[:200]}]


# --- диспетчер для фото ----------------------------------------------------------------------------


def _photo_type(env: Env, event: IntakeEvent, images, caption: str) -> tuple[str, float, str]:
    if env.gateway is None or not env.gateway.has_capability("vision", "vision"):
        return "site_photo", 0.5, "rules"
    res = und.see_photo(env.gateway, images[0], caption, hints=hints(env, "vision"))
    if res.data is None:
        return "site_photo", 0.4, "rules"
    event.result = {**event.result, "vision": res.data.model_dump()}
    if res.data.is_document:
        t = (res.data.document_type or "").lower()
        for key, words in (("upd", ("накладн", "упд", "ттн", "торг")), ("certificate", ("сертификат", "паспорт", "качеств")),
                           ("test_protocol", ("протокол",))):
            if any(w in t for w in words):
                return key, 0.8, res.provider or "vision"
        return "unknown", 0.5, res.provider or "vision"
    return "site_photo", 0.85, res.provider or "vision"


# --- специалисты ---------------------------------------------------------------------------------


def _register(env: Env, event: IntakeEvent, dtype: str, title: str, specialist: str = "dispatcher",
              data: dict[str, Any] | None = None) -> AIAction | None:
    if not event.file_key and not event.text:
        return None
    return _act(env, event, specialist, "register_document", f"Документ: {title}",
                f"Сохранён в реестре объекта и в базе знаний как «{und.TYPES.get(dtype, dtype)}».",
                {"doc_kind": DOC_KIND.get(dtype, "other"), "title": title, "data": data or {},
                 "knowledge_kind": KNOWLEDGE_KIND.get(dtype, "org_document")}, 0.95, evidence=_file_evidence(event))


def _supply(env: Env, event: IntakeEvent, text: str, images) -> None:
    res = und.read_supply(env.gateway, text, images, hints=hints(env, "reader"))
    doc = res.data
    method = "по правилам" if res.used_fallback else f"моделью {res.provider}"
    ref = (f"УПД № {doc.number or 'б/н'}" + (f" от {doc.date:%d.%m.%Y}" if doc.date else "")) if doc else "Накладная"
    _register(env, event, "upd", ref + (f", {doc.supplier}" if doc and doc.supplier else ""), "reader",
              data=doc.model_dump(mode="json") if doc else None)
    if not doc or not doc.items:
        _act(env, event, "reader", "flag_issue", "Не удалось прочитать позиции накладной",
             "Позиции не распознаны — проверьте качество скана или внесите приход вручную.", {"audience": "supply"}, 0.9,
             severity="warning", evidence=_file_evidence(event))
        return
    supplier = None
    if doc.supplier_inn:
        supplier = env.db.scalar(select(Counterparty).where(Counterparty.organization_id == env.org_id,
                                                            Counterparty.inn == doc.supplier_inn))
    for item in doc.items:
        mat, score = match_material(env, item.name, item.unit)
        conf = (0.92 if not res.used_fallback else 0.82) * (max(score, 0.8) if mat else 0.75)
        price = f" Цена {item.price:,.2f} руб./{item.unit}.".replace(",", " ") if item.price else ""
        _act(env, event, "reader", "create_receipt", f"Приход: {item.name} — {fmt(item.quantity)} {item.unit}",
             f"{ref}{f', {doc.supplier}' if doc.supplier else ''}. "
             + (f"Материал в справочнике: «{mat.name}» (совпадение {score:.0%})." if mat else
                "Такого материала нет в справочнике — будет добавлен.") + price + f" Прочитано {method}.",
             {"object_id": event.object_id, "material_id": mat.id if mat else None,
              "new_material": None if mat else {"name": item.name, "unit": item.unit}, "source_name": item.name,
              "quantity": item.quantity, "price": item.price, "date": doc.date or dt.date.today(), "document_ref": ref,
              "supplier_id": supplier.id if supplier else None,
              "quality_doc": "; ".join(doc.certificates_mentioned)[:500] if doc.certificates_mentioned else None},
             conf, evidence=_file_evidence(event) + ([{"type": "material", "id": str(mat.id), "label": mat.name}] if mat else []))
    if not doc.certificates_mentioned:
        _act(env, event, "reader", "create_task", f"Запросить документы о качестве по {ref}",
             "В накладной нет ссылок на сертификаты/паспорта — без них материалы не войдут в АОСР.",
             {"title": f"Запросить у поставщика документы о качестве по {ref}", "role": "supply", "due_days": 2,
              "object_id": event.object_id}, 0.9)


def _quality(env: Env, event: IntakeEvent, text: str, images) -> None:
    res = und.read_quality(env.gateway, text, images, hints=hints(env, "reader"))
    q = res.data
    label = {"passport": "Паспорт качества", "declaration": "Декларация"}.get(q.doc_type if q else "", "Сертификат")
    title = (f"{label} № {q.number or 'б/н'}" + (f" от {q.date:%d.%m.%Y}" if q.date else "")) if q else "Документ о качестве"
    _register(env, event, "certificate", title + (f" — {q.material}" if q and q.material else ""), "reader",
              data=q.model_dump(mode="json") if q else None)
    if not q:
        return
    if q.valid_until and q.valid_until < dt.date.today():
        _act(env, event, "verifier", "flag_issue", f"Истёк срок действия: {title}",
             f"Документ действителен до {q.valid_until:%d.%m.%Y} — материал нельзя предъявлять в ИД.", {"audience": "supply"},
             0.95, severity="warning", evidence=_file_evidence(event))
    if not q.material:
        return
    stmt = select(MaterialReceipt, Material).join(Material, Material.id == MaterialReceipt.material_id).where(
        MaterialReceipt.organization_id == env.org_id, MaterialReceipt.quality_doc.is_(None))
    if event.object_id:
        stmt = stmt.where(MaterialReceipt.object_id == event.object_id)
    receipts = [(r, m) for r, m in env.db.execute(stmt).all() if wsvc.similarity(q.material, m.name) >= 0.35]
    text_ref = title + (f", партия {q.batch}" if q.batch else "")
    if receipts:
        _act(env, event, "reader", "link_certificate", f"{label} привязан к поступлениям: {receipts[0][1].name}",
             f"{text_ref} → {len(receipts)} поступл. без документа о качестве "
             f"({', '.join(r.document_ref or '' for r, _ in receipts[:3])}).",
             {"receipt_ids": [r.id for r, _ in receipts], "text": text_ref}, 0.85 if not res.used_fallback else 0.75,
             evidence=_file_evidence(event) + [{"type": "receipt", "id": str(r.id), "label": r.document_ref or m.name}
                                               for r, m in receipts[:5]])
    else:
        _act(env, event, "reader", "info", f"{label} сохранён: {q.material}",
             "Поступлений этого материала без документа о качестве нет — привяжется, когда придёт накладная.", {}, 0.9)


def _protocol(env: Env, event: IntakeEvent, text: str, images) -> None:
    res = und.read_protocol(env.gateway, text, images, hints=hints(env, "reader"))
    p = res.data
    title = (f"Протокол испытаний № {p.number or 'б/н'}" + (f": {p.structure}" if p.structure else "")) if p else "Протокол испытаний"
    _register(env, event, "test_protocol", title, "reader", data=p.model_dump(mode="json") if p else None)
    if p and p.compliant is False:
        _act(env, event, "verifier", "flag_issue", f"Несоответствие по протоколу: {p.structure or title}",
             f"{p.test or 'Испытание'}: требуется {p.required or '—'}, фактически {p.actual or '—'}. Конструкцию нельзя "
             "предъявлять к приёмке до решения проектировщика.", {"audience": "pto"}, 0.95, severity="critical",
             evidence=_file_evidence(event))
        _act(env, event, "verifier", "create_task", f"Решение по несоответствию: {p.structure or title}",
             "Согласовать с проектировщиком дополнительные испытания или усиление.",
             {"title": f"Несоответствие по протоколу № {p.number or 'б/н'}: {p.structure or ''} — согласовать решение",
              "role": "pto", "due_days": 3, "priority": "high", "object_id": event.object_id}, 0.9)
    elif p and p.compliant:
        _act(env, event, "reader", "info", f"Испытания подтверждают соответствие: {p.structure or title}",
             f"{p.test or 'Испытание'}: {p.actual or ''} при требовании {p.required or '—'}. Протокол войдёт в ИД.", {}, 0.9)


def _report(env: Env, event: IntakeEvent, text: str, images) -> None:
    spec = "hearing" if event.kind == "voice" else "dispatcher"
    res = und.read_report(env.gateway, text, hints=hints(env, spec))
    r = res.data or und.FieldReport()
    complete = bool(COMPLETE_RE.search(text))
    matched: list[EstimateItem] = []
    for w in r.works:
        item, score = match_work(env, w.description, w.unit, event.object_id)
        if item is not None and w.quantity and score >= 0.2:
            matched.append(item)
        if item is None or not w.quantity or score < 0.2:
            _act(env, event, spec, "flag_issue", f"Не сопоставлено со сметой: {w.description[:120]}",
                 ("Не указан объём. " if not w.quantity else "")
                 + "Не найдена позиция сметы с такой единицей — исправьте позицию в ленте, ИИ запомнит.",
                 {"audience": "pto"}, 0.8, severity="warning", evidence=_file_evidence(event))
            continue
        _act(env, event, spec, "report_volume", f"{item.name}: {fmt(w.quantity)} {item.unit}" + (f", {w.location}" if w.location else ""),
             f"Из сообщения: «{w.description[:200]}». Позиция сметы {item.code or ''} (совпадение {score:.0%}).",
             {"object_id": event.object_id or item.object_id, "estimate_item_id": item.id, "quantity": w.quantity,
              "location": w.location, "date": dt.date.today(), "note": w.description[:500], "source_text": w.description,
              "complete": complete},
             min(0.95, 0.55 + score * 0.6),
             evidence=_file_evidence(event) + [{"type": "estimate_item", "id": str(item.id),
                                                "label": f"{item.code or ''} {item.name}".strip()}])
    prefer = set(env.db.scalars(select(MaterialNorm.material_id).where(
        MaterialNorm.estimate_item_id.in_([i.id for i in matched])))) if matched else set()
    for m in r.materials:
        mat, score = match_material(env, m.name, m.unit, prefer=prefer)
        if mat is None:
            _act(env, event, spec, "flag_issue", f"Материал не найден в справочнике: {m.name}",
                 f"Расход {m.quantity:g} {m.unit} не учтён — выберите материал, ИИ запомнит название.", {"audience": "supply"},
                 0.8, severity="warning")
            continue
        _act(env, event, spec, "record_usage", f"Расход: {mat.name} — {fmt(m.quantity)} {mat.unit}",
             f"Из сообщения прораба. Совпадение со справочником {score:.0%}"
             + (" — материал по норме работы из этого сообщения." if mat.id in prefer else "."),
             {"object_id": event.object_id, "material_id": mat.id, "quantity": m.quantity, "date": dt.date.today(),
              "note": "из отчёта с площадки", "source_name": m.name}, min(0.95, 0.5 + score * 0.6),
             evidence=[{"type": "material", "id": str(mat.id), "label": mat.name}])
    for p in r.problems:
        _act(env, event, spec, "create_task", f"Проблема на объекте: {p[:150]}", "Поручение руководителю участка.",
             {"title": p[:500], "role": "manager", "due_days": 1, "priority": "high", "object_id": event.object_id}, 0.85,
             evidence=_file_evidence(event))
    if not (r.works or r.materials or r.problems):
        _act(env, event, spec, "info", "Сообщение сохранено", "Работ, расхода или проблем не найдено.", {}, 0.9)


def _photo(env: Env, event: IntakeEvent, text: str, images) -> None:
    v = (event.result or {}).get("vision")
    _register(env, event, "site_photo", f"Фото с объекта {dt.date.today():%d.%m.%Y}" + (f": {text[:80]}" if text else ""),
              "vision", data={"vision": v} if v else None)
    if not v:
        _act(env, event, "vision", "info", "Фото сохранено без анализа",
             "Модель зрения не подключена: фото прикреплено к объекту. С моделью ИИ определит вид работ, готовность, дефекты "
             "и нарушения охраны труда.", {}, 1.0)
        return
    _act(env, event, "vision", "info", f"На фото: {v.get('work_type') or 'площадка'}"
         + (f", готовность ~{v['completion_percent']}%" if v.get("completion_percent") is not None else ""),
         v.get("description") or "", {"vision": v}, 0.8, evidence=_file_evidence(event))
    for d in v.get("defects") or []:
        sev = {"high": "critical", "medium": "warning"}.get(d.get("severity"), "info")
        _act(env, event, "vision", "flag_issue", f"Дефект: {d['description'][:150]}", "Обнаружено на фото — проверьте на месте.",
             {"audience": "pto"}, 0.75, severity=sev, evidence=_file_evidence(event))
    for s in v.get("safety_violations") or []:
        _act(env, event, "vision", "flag_issue", f"Нарушение охраны труда: {s[:150]}", "Обнаружено на фото с площадки.",
             {"audience": "foreman"}, 0.75, severity="critical", evidence=_file_evidence(event))
        _act(env, event, "vision", "create_task", f"Устранить нарушение ОТ: {s[:150]}", "",
             {"title": f"Устранить нарушение охраны труда: {s[:400]}", "role": "foreman", "due_days": 0,
              "priority": "high", "object_id": event.object_id}, 0.75)


def _survey(env: Env, event: IntakeEvent, text: str, images) -> None:
    from bis.cad.scheme import SchemeData, check_points, parse_survey, summary

    pts = parse_survey(env.storage.get(event.file_key), event.file_name or "survey.csv")
    stem = re.sub(r"^(?:исп\w*[\s_-]*)?(?:съ[её]мк\w*|survey)[\s_-]*", "", Path(event.file_name or "").stem,
                  flags=re.IGNORECASE).replace("_", " ").strip()
    data = SchemeData(title=f"Исполнительная схема{': ' + stem if stem else ''}", points=pts,
                      date=f"{dt.date.today():%d.%m.%Y}")
    info = summary(check_points(data), data)
    _register(env, event, "survey", f"Геодезическая съёмка: {Path(event.file_name or 'съёмка').stem}", "surveyor")
    _act(env, event, "surveyor", "build_scheme", f"Исполнительная схема по съёмке ({len(pts)} {plural(len(pts), ('точка', 'точки', 'точек'))})",
         f"{info['conclusion']}. Макс. отклонение в плане {info['max_plan_mm']} мм"
         + (f", по высоте {info['max_height_mm']} мм" if info["max_height_mm"] is not None else "")
         + ". Оси и контур в съёмке не заданы — добавьте в схеме перед подписанием.",
         {"scheme": data.model_dump(mode="json"), "object_id": event.object_id}, 0.9,
         severity="warning" if info["out_of_tolerance"] else "info", evidence=_file_evidence(event))
    if info["out_of_tolerance"]:
        bad = info["out_of_tolerance"]
        _act(env, event, "surveyor", "flag_issue", f"Превышение допусков в {'точке' if len(bad) == 1 else 'точках'} {', '.join(bad[:6])}",
             f"Допуск в плане {info['tolerance_plan_mm']:g} мм, по высоте {info['tolerance_height_mm']:g} мм. Нужна оценка "
             "проектировщика до освидетельствования.", {"audience": "pto"}, 0.95, severity="warning")


def _letter(env: Env, event: IntakeEvent, text: str, images) -> None:
    _register(env, event, "letter", Path(event.file_name or "Входящее письмо").stem, "secretary")
    _act(env, event, "secretary", "process_letter", "Разбор входящего письма",
         "Регистрация, суть, срок ответа, поручения ответственным.", {}, 0.9, evidence=_file_evidence(event))


ROLE_WORDS = {"заказчик": "customer", "покупатель": "customer", "генподрядчик": "?", "генеральный подрядчик": "?",
              "подрядчик": "contractor", "субподрядчик": "contractor", "поставщик": "contractor", "исполнитель": "contractor",
              "проектировщик": "contractor"}


def contract_role(text: str, org: Organization | None, kind: str) -> str:
    """Наша роль в договоре — по тому, как названа наша организация («именуемое в дальнейшем …»)."""
    low = text.lower()
    marks = []
    if org is not None:
        marks = [m for m in (org.inn or "", (org.name or "").split("«")[-1].split("»")[0].lower()) if len(m) >= 4]
    for mark in marks:
        pos = low.find(mark)
        if pos < 0:
            continue
        m = re.search(r"именуем\w*\s+(?:в\s+дальнейшем\s+)?[«\"]?([а-яё ]+?)[»\"]", low[pos:pos + 400])
        if m:
            role = ROLE_WORDS.get(m.group(1).strip())
            if role == "?":
                return "customer" if kind == "subcontract" else "contractor"
            if role:
                return role
    return "contractor"


def _contract(env: Env, event: IntakeEvent, text: str, images) -> None:
    head = text.lower()[:800]
    kind = "supply" if "поставк" in head else "subcontract" if "субподряд" in head else \
        "design" if "проектн" in head or "проектир" in head else "services" if "оказани" in head else "general"
    org = env.db.get(Organization, env.org_id)
    our_role = contract_role(text, org, kind)
    _register(env, event, "contract", Path(event.file_name or "Договор").stem, "lawyer")
    titles = {"general": "генподряд", "subcontract": "субподряд", "supply": "поставка", "design": "проектирование",
              "services": "услуги"}
    _act(env, event, "lawyer", "import_contract", f"Новый договор ({titles[kind]}): поставить обязательства на контроль",
         f"Наша роль: {'подрядчик/исполнитель' if our_role == 'contractor' else 'заказчик'}. После утверждения юрист "
         "извлечёт сроки, суммы, неустойки и риски, а обязательства встанут на контроль с напоминаниями.",
         {"kind": kind, "our_role": our_role, "object_id": event.object_id}, 0.7, evidence=_file_evidence(event))


def _project(env: Env, event: IntakeEvent, text: str, images) -> None:
    from bis.services.project_docs import compare_with_estimate, detect_section, heuristic_spec

    sec = detect_section(event.file_name or "", text[:3000])
    _register(env, event, "project_doc", Path(event.file_name or "Проектный документ").stem, "pto", data={"pd_section": sec})
    spec = heuristic_spec(text)
    contract = env.db.scalar(select(Contract).where(Contract.organization_id == env.org_id, Contract.object_id == event.object_id,
                                                    Contract.kind == "general")) if event.object_id else None
    if spec.items and contract:
        cmp = compare_with_estimate(env.db, env.org_id, contract.id, spec)
        for m in cmp["mismatches"]:
            _act(env, event, "economist", "flag_issue", f"Проект ≠ смета: {m['estimate_item'][:100]}",
                 f"По проекту {m['spec_quantity']:g} {m['unit']}, в смете {m['estimate_quantity']:g} ({m['difference_pct']}%).",
                 {"audience": "pto"}, 0.85, severity="warning", evidence=_file_evidence(event))
        for m in cmp["not_in_estimate"]:
            _act(env, event, "economist", "flag_issue", f"Нет в смете: {m['name'][:100]}",
                 f"{m['quantity']:g} {m['unit']} по проекту — риск неоплачиваемых работ, нужно допсоглашение.",
                 {"audience": "manager"}, 0.85, severity="warning", evidence=_file_evidence(event))
    _act(env, event, "pto", "info", f"Раздел {sec.get('code') or 'не определён'}" + (f" ({sec['title']})" if sec.get("title") else ""),
         f"Позиций спецификации: {len(spec.items)}. Документ доступен специалистам для нормоконтроля и ответов.", {}, 0.9)


def _unknown(env: Env, event: IntakeEvent, text: str, images) -> None:
    _register(env, event, "unknown", Path(event.file_name or "Документ").stem)
    _act(env, event, "dispatcher", "info", "Тип не определён — сохранено",
         "Документ сохранён в базе знаний; уточните, что это, если нужна обработка.", {}, 0.9)


PIPELINES: dict[str, Callable[[Env, IntakeEvent, str, Any], None]] = {
    "upd": _supply, "certificate": _quality, "test_protocol": _protocol, "foreman_report": _report, "site_photo": _photo,
    "survey": _survey, "letter": _letter, "contract": _contract, "project_doc": _project, "unknown": _unknown,
}


# --- контролёр -------------------------------------------------------------------------------------


def verify(env: Env, event: IntakeEvent | None, actions: list[AIAction]) -> None:
    """Сверяет применённые действия с другими источниками и запускает сборку документов."""
    for a in actions:
        if a.status != "applied":
            continue
        if a.kind == "create_receipt" and a.payload.get("price"):
            rec = env.db.get(MaterialReceipt, uuid.UUID(a.result["receipt_id"]))
            total, qty = env.db.execute(
                select(func.sum(MaterialReceipt.quantity * MaterialReceipt.price), func.sum(MaterialReceipt.quantity))
                .where(MaterialReceipt.organization_id == env.org_id, MaterialReceipt.material_id == rec.material_id,
                       MaterialReceipt.id != rec.id, MaterialReceipt.price.is_not(None))).one()
            if qty:
                avg = float(total / qty)
                if float(rec.price) > avg * 1.15:
                    name = env.db.get(Material, rec.material_id).name
                    _act(env, event, "verifier", "flag_issue", f"Цена выше обычной: {name}",
                         (f"{float(rec.price):,.2f} руб. против средней {avg:,.2f} руб. "
                          f"(+{(float(rec.price) / avg - 1) * 100:.0f}%) — проверьте договор поставки.").replace(",", " "),
                         {"audience": "manager", "object_id": rec.object_id}, 0.9, severity="warning",
                         evidence=[{"type": "receipt", "id": str(rec.id), "label": rec.document_ref}])
        elif a.kind == "record_usage":
            _check_norm(env, event, a, actions)
            bal = next((b for b in wsvc.material_balance(env.db, env.org_id, uuid.UUID(a.payload["object_id"]))
                        if b["material_id"] == a.payload["material_id"]), None)
            if bal and bal["balance"] < 0:
                _act(env, event, "verifier", "flag_issue", f"Расход больше прихода: {bal['material']}",
                     f"Остаток {fmt(bal['balance'])} {bal['unit']}: нет документов на поступление или ошибка в расходе.",
                     {"audience": "supply", "object_id": a.payload["object_id"]}, 0.9, severity="warning")
        elif a.kind == "report_volume":
            item = env.db.get(EstimateItem, uuid.UUID(a.payload["estimate_item_id"]))
            object_id = uuid.UUID(a.payload["object_id"])
            done = Decimal(env.db.scalar(select(func.coalesce(func.sum(WorkVolume.quantity), 0)).where(
                WorkVolume.estimate_item_id == item.id)) or 0)
            before = done - Decimal(str(a.payload["quantity"]))
            if done > item.quantity >= before:
                _act(env, event, "verifier", "flag_issue", f"Сверх сметы: {item.name[:100]}",
                     f"Накоплено {fmt(done)} {item.unit} при сметных {fmt(item.quantity)} — нужно допсоглашение или проверка объёмов.",
                     {"audience": "manager", "object_id": object_id}, 0.9, severity="warning",
                     evidence=[{"type": "estimate_item", "id": str(item.id), "label": item.name}])
            if item.requires_aosr and (a.payload.get("complete") or done >= item.quantity):
                assemble_aosr(env, event, item, object_id, location=a.payload.get("location") if a.payload.get("complete") else None)


def _check_norm(env: Env, event: IntakeEvent | None, usage: AIAction, actions: list[AIAction]) -> None:
    """Расход против нормы на объём, сообщённый в том же сигнале (контроль списания материалов)."""
    mat_id = uuid.UUID(usage.payload["material_id"])
    norm_total, parts = Decimal(0), []
    for v in actions:
        if v.kind != "report_volume" or v.status != "applied":
            continue
        norm = env.db.scalar(select(MaterialNorm).where(MaterialNorm.estimate_item_id == uuid.UUID(v.payload["estimate_item_id"]),
                                                        MaterialNorm.material_id == mat_id))
        if norm:
            qty = Decimal(str(v.payload["quantity"]))
            norm_total += qty * norm.rate * (1 + norm.loss_percent / 100)
            parts.append(f"{fmt(qty)} × {fmt(norm.rate)}" + (f" + {fmt(norm.loss_percent)}% потерь" if norm.loss_percent else ""))
    if not norm_total:
        return
    used = Decimal(str(usage.payload["quantity"]))
    diff = float((used - norm_total) / norm_total * 100)
    unit = env.db.get(Material, mat_id).unit
    usage.summary = ((usage.summary or "") + f" По норме {fmt(norm_total)} {unit} ({'; '.join(parts)}): "
                     + ("в пределах." if diff <= 2 else f"перерасход {diff:+.1f}%.")).strip()
    if diff > 2:
        name = env.db.get(Material, mat_id).name
        _act(env, event, "verifier", "flag_issue", f"Перерасход к норме: {name} {diff:+.1f}%",
             f"Списано {fmt(used)} {unit} при норме {fmt(norm_total)} {unit} на сообщённый объём — без объяснения не войдёт "
             "в акт списания; уточните у прораба (потери, брак, ошибка в объёме).", {"audience": "supply",
                                                                                    "object_id": usage.payload["object_id"]},
             0.9, severity="warning")


# --- инженер ПТО: АОСР из фактов -------------------------------------------------------------------


def _loc_key(v: str | None) -> str:
    return re.sub(r"[^\w]+", " ", (v or "").lower().replace("№", "")).strip()


def assemble_aosr(env: Env, event: IntakeEvent | None, item: EstimateItem, object_id: uuid.UUID, *,
                  location: str | None = None) -> AIAction | None:
    """Собирает черновик АОСР по скрытой работе из того, что уже знает система.

    location — захватка/оси из сообщения о завершении: акт только на этот участок, а не на всё накопленное."""
    from bis.rag.search import hybrid_search

    drafts = env.db.scalars(select(Document).where(Document.organization_id == env.org_id, Document.kind == "aosr",
                                                   Document.status == "draft", Document.object_id == object_id)).all()
    if any((d.data or {}).get("estimate_item_id") == str(item.id) and (d.data or {}).get("location") == _loc_key(location)
           for d in drafts):
        return None  # черновик по этой работе уже ждёт подписи
    vols = env.db.scalars(select(WorkVolume).where(WorkVolume.estimate_item_id == item.id, WorkVolume.object_id == object_id,
                                                   WorkVolume.aosr_document_id.is_(None)).order_by(WorkVolume.date)).all()
    if location:
        here = [v for v in vols if _loc_key(v.location) == _loc_key(location)]
        vols = here or vols
    if not vols:
        return None
    start, end = vols[0].date, vols[-1].date
    locations = sorted({v.location for v in vols if v.location})
    materials: list[str] = []
    missing: list[str] = []
    evidence: list[dict[str, Any]] = []
    for norm in env.db.scalars(select(MaterialNorm).where(MaterialNorm.estimate_item_id == item.id)):
        mat = env.db.get(Material, norm.material_id)
        recs = env.db.scalars(select(MaterialReceipt).where(MaterialReceipt.material_id == mat.id,
                                                            MaterialReceipt.object_id == object_id,
                                                            MaterialReceipt.date <= end).order_by(MaterialReceipt.date)).all()
        with_doc = [r for r in recs if r.quality_doc]
        if with_doc:
            materials.append(f"{mat.name} ({', '.join(r.document_ref or '' for r in with_doc[:2])}): "
                             f"{'; '.join(r.quality_doc for r in with_doc[:2])}")
            evidence += [{"type": "receipt", "id": str(r.id), "label": r.document_ref or mat.name} for r in with_doc[:2]]
        else:
            missing.append(mat.name)
            materials.append(f"{mat.name} [нет документа о качестве — запросить]")
    exec_docs = [d.title for d in env.db.scalars(select(Document).where(
        Document.organization_id == env.org_id, Document.object_id == object_id, Document.status != "rejected",
        Document.kind.in_(["scheme", "test_protocol"])).order_by(Document.created_at.desc()).limit(4))]
    rd = env.db.scalars(select(Document).where(Document.organization_id == env.org_id, Document.object_id == object_id,
                                               Document.kind == "project_doc", Document.status != "rejected")).all()
    rd_best = max(rd, key=lambda d: wsvc.similarity(item.name, d.title), default=None)
    try:
        norms = [h.citation for h in hybrid_search(env.db, env.embedder, item.name, org_id=env.org_id, kinds=["norm"], k=2)]
    except Exception:  # noqa: BLE001 — без норм черновик всё равно полезен
        norms = []
    tasks = env.db.scalars(select(ScheduleTask).where(ScheduleTask.object_id == object_id).order_by(ScheduleTask.planned_start)).all()
    idx = next((i for i, t in enumerate(tasks) if t.estimate_item_id == item.id), None)
    next_works = tasks[idx + 1].name if idx is not None and idx + 1 < len(tasks) else None
    payload = {
        "object_id": object_id, "estimate_item_id": item.id, "volume_ids": [v.id for v in vols], "location": _loc_key(location),
        "works": item.name + (f" ({', '.join(locations)})" if locations else ""),
        "project_docs": rd_best.title if rd_best else "[уточнить шифр и листы РД]",
        "materials": materials, "executive_docs": exec_docs or ["[приложить исполнительную схему]"],
        "work_start": start, "work_end": end, "norms": norms, "next_works": next_works, "act_date": max(end, dt.date.today()),
    }
    gaps = (["документы о качестве: " + ", ".join(missing)] if missing else []) + \
        ([] if exec_docs else ["исполнительная схема"]) + ([] if rd_best else ["шифр РД"])
    total = sum((v.quantity for v in vols), Decimal(0))
    period = f"{start:%d.%m.%Y}" if start == end else f"{start:%d.%m}–{end:%d.%m.%Y}"
    return _act(env, event, "pto", "draft_aosr", f"АОСР собран из фактов: {item.name[:110]}" + (f", {location}" if location else ""),
                f"{len(vols)} {plural(len(vols), ('запись', 'записи', 'записей'))} объёмов ({fmt(total)} {item.unit}) за {period}; материалы и документы "
                f"о качестве — из поступлений; нормы — {', '.join(norms) or 'не найдены в базе'}."
                + (f" Не хватает: {'; '.join(gaps)}." if gaps else " Данных достаточно — на подпись."),
                payload, 0.85 if not gaps else 0.72, severity="warning" if gaps else "info",
                evidence=[{"type": "estimate_item", "id": str(item.id), "label": item.name}] + evidence)


# --- применение и откат ----------------------------------------------------------------------------


def _date(v: Any) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v)) if v else dt.date.today()


def _actor_user(env: Env) -> User:
    if env.user is not None:
        return env.user
    return env.db.scalar(select(User).where(User.organization_id == env.org_id, User.role == "admin").order_by(User.created_at))


def _event(env: Env, a: AIAction) -> IntakeEvent | None:
    return env.db.get(IntakeEvent, a.event_id) if a.event_id else None


def _apply_register(env: Env, a: AIAction) -> dict:
    from bis.rag.ingest import ingest_bytes

    ev = _event(env, a)
    p = a.payload
    if not ev.file_key:  # текстовое сообщение — сохраняем как файл, чтобы документ был в реестре
        ev.file_key = env.storage.put((ev.text or "").encode("utf-8"), "message.txt", prefix=f"intake/{env.org_id}")
        ev.file_name, ev.mime = ev.file_name or "Сообщение.txt", "text/plain"
    doc = Document(organization_id=env.org_id, kind=p["doc_kind"], title=p["title"][:900], status="registered",
                   source=ev.source if ev.source in ("email", "telegram") else "uploaded", object_id=ev.object_id,
                   file_key=ev.file_key, file_name=ev.file_name, mime=ev.mime,
                   data={**(p.get("data") or {}), "intake_event_id": str(ev.id)}, issues=[],
                   created_by=f"agent:{a.specialist}", date=dt.date.today())
    env.db.add(doc)
    env.db.flush()
    ev.document_id = doc.id
    if ev.kind in ("document", "text"):
        try:
            with env.db.begin_nested():
                ingest_bytes(env.db, env.storage, env.embedder, org_id=env.org_id, data=env.storage.get(ev.file_key),
                             filename=ev.file_name or "file", kind=p.get("knowledge_kind", "org_document"), title=doc.title,
                             document_id=doc.id, mime=ev.mime)
        except (UnsupportedFormat, ValueError) as e:
            return {"document_id": str(doc.id), "knowledge": f"не проиндексирован: {e}"}
    return {"document_id": str(doc.id)}


def _ensure_document(env: Env, ev: IntakeEvent) -> Document:
    if ev.document_id is None:
        reg = env.db.scalar(select(AIAction).where(AIAction.event_id == ev.id, AIAction.kind == "register_document",
                                                   AIAction.status.in_(["new", "proposed", "failed"])))
        if reg is None:
            raise ValueError("Документ не зарегистрирован")
        apply_action(env, reg, auto=True)
        if reg.status != "applied":
            raise ValueError(f"Не удалось зарегистрировать документ: {reg.result.get('error')}")
    return env.db.get(Document, ev.document_id)


def _apply_receipt(env: Env, a: AIAction) -> dict:
    p = a.payload
    material_id, created = _uuid(p.get("material_id")), False
    if material_id is None:
        nm = p["new_material"]
        mat = Material(organization_id=env.org_id, name=nm["name"][:500], unit=nm["unit"], category="Из накладных (ИИ)")
        env.db.add(mat)
        env.db.flush()
        material_id, created = mat.id, True
    r = MaterialReceipt(organization_id=env.org_id, object_id=_uuid(p["object_id"]), material_id=material_id,
                        date=_date(p.get("date")), quantity=Decimal(str(p["quantity"])),
                        price=Decimal(str(p["price"])) if p.get("price") is not None else None,
                        document_ref=p.get("document_ref"), quality_doc=p.get("quality_doc"),
                        supplier_id=_uuid(p.get("supplier_id")))
    env.db.add(r)
    env.db.flush()
    return {"receipt_id": str(r.id), "material_id": str(material_id), "material_created": created}


def _apply_volume(env: Env, a: AIAction) -> dict:
    p = a.payload
    spec = BY_NAME.get(a.specialist)
    v = WorkVolume(organization_id=env.org_id, object_id=_uuid(p["object_id"]), estimate_item_id=_uuid(p["estimate_item_id"]),
                   date=_date(p.get("date")), quantity=Decimal(str(p["quantity"])), location=p.get("location"),
                   note=p.get("note"), status="reported", reported_by=f"ИИ ({spec.title})" if spec else "ИИ")
    env.db.add(v)
    env.db.flush()
    return {"volume_id": str(v.id)}


def _apply_usage(env: Env, a: AIAction) -> dict:
    p = a.payload
    u = MaterialUsage(organization_id=env.org_id, object_id=_uuid(p["object_id"]), material_id=_uuid(p["material_id"]),
                      date=_date(p.get("date")), quantity=Decimal(str(p["quantity"])), note=p.get("note"))
    env.db.add(u)
    env.db.flush()
    return {"usage_id": str(u.id)}


def _apply_certificate(env: Env, a: AIAction) -> dict:
    ids = []
    for rid in a.payload["receipt_ids"]:
        r = env.db.get(MaterialReceipt, _uuid(rid))
        if r and r.organization_id == env.org_id and not r.quality_doc:
            r.quality_doc = a.payload["text"][:500]
            ids.append(str(r.id))
    return {"receipt_ids": ids}


def _apply_task(env: Env, a: AIAction) -> dict:
    from bis.services.correspondence import assignee_for_role

    p = a.payload
    org = env.db.get(Organization, env.org_id)
    person, user = assignee_for_role(env.db, org, p.get("role", "manager"), None)
    t = Task(organization_id=env.org_id, title=p["title"][:1000], description=a.summary or None,
             due_date=dt.date.today() + dt.timedelta(days=int(p.get("due_days", 2))), priority=p.get("priority", "normal"),
             assignee_person_id=person.id if person else None, assignee_user_id=user.id if user else None,
             source_type="agent", source_id=str(a.id), object_id=_uuid(p.get("object_id")), created_by=f"agent:{a.specialist}")
    env.db.add(t)
    env.db.flush()
    if user:
        notify(env.db, env.org_id, [user], title="Поручение от ИИ", body=t.title, entity_type="task", entity_id=str(t.id),
               key=f"task:{t.id}:new", level="warning" if t.priority == "high" else "info")
    return {"task_id": str(t.id), "assignee": user.full_name if user else None}


def _apply_flag(env: Env, a: AIAction) -> dict:
    if a.severity == "info":
        return {"notified": 0}
    audience = a.payload.get("audience", "")
    if audience == "admins":
        users = managers(env.db, env.org_id, ("admin",))
    else:
        role = {"supply": "supply", "pto": "pto", "foreman": "foreman"}.get(audience)
        users = list(env.db.scalars(select(User).where(User.organization_id == env.org_id, User.role == role,
                                                       User.is_active.is_(True)))) if role else []
        if not users or a.severity == "critical" or audience == "manager":
            users += managers(env.db, env.org_id, ("admin", "manager"))
    n = notify(env.db, env.org_id, users, title=a.title, body=a.summary, level=a.severity, entity_type="ai_action",
               entity_id=str(a.id), key=f"ai:{a.id}")
    return {"notified": n}


def _apply_aosr(env: Env, a: AIAction) -> dict:
    from bis.agents.base import AgentContext
    from bis.agents.builtin_tools import AOSRArgs, create_aosr_draft

    p = a.payload
    args = AOSRArgs(object_id=str(p["object_id"]), works=p["works"], project_docs=p["project_docs"], materials=p["materials"],
                    executive_docs=p["executive_docs"], work_start=p["work_start"], work_end=p["work_end"], norms=p["norms"],
                    next_works=p.get("next_works"), act_date=p.get("act_date"))
    ctx = AgentContext(db=env.db, org_id=env.org_id, user=_actor_user(env), gateway=env.gateway, embedder=env.embedder,
                       storage=env.storage, agent_name="pto")
    ref = create_aosr_draft.handler(ctx, args)
    doc = env.db.get(Document, uuid.UUID(ref["id"]))
    doc.data = {**(doc.data or {}), "estimate_item_id": str(p["estimate_item_id"]), "ai_action_id": str(a.id),
                "location": p.get("location") or ""}
    for vid in p.get("volume_ids", []):
        v = env.db.get(WorkVolume, _uuid(vid))
        if v and v.aosr_document_id is None:
            v.aosr_document_id = doc.id
    return {"document_id": ref["id"], "issues": ref.get("issues", [])}


def _apply_scheme(env: Env, a: AIAction) -> dict:
    from bis.cad.scheme import SchemeData
    from bis.services.schemes import create_scheme

    data = SchemeData.model_validate(a.payload["scheme"])
    org = env.db.get(Organization, env.org_id)
    obj = env.db.get(ConstructionObject, _uuid(a.payload["object_id"])) if a.payload.get("object_id") else None
    data.organization = data.organization or org.name
    data.object_name = data.object_name or (obj.name if obj else "")
    doc = create_scheme(env.db, env.storage, env.org_id, data, actor=f"agent:{a.specialist}", object_id=obj.id if obj else None)
    return {"document_id": str(doc.id)}


ROLE_RU = {"pto": "ПТО", "lawyer": "юрист", "supply": "снабжение", "foreman": "прораб", "manager": "руководитель",
           "secretary": "секретарь"}


def letter_outcome(doc: Document) -> tuple[str, str]:
    """Заголовок и суть разбора письма секретарём — для ленты и ответа в Telegram."""
    an = (doc.data or {}).get("analysis") or {}
    tasks = (doc.data or {}).get("tasks") or []
    parts = [an.get("summary") or an.get("subject") or ""]
    if an.get("response_required"):
        due = an.get("response_due")
        parts.append("Нужен ответ" + (f" до {dt.date.fromisoformat(due):%d.%m.%Y}" if due else "") + ".")
    roles = sorted({ROLE_RU.get(r.get("role"), r.get("role")) for r in an.get("requests") or []})
    if tasks:
        parts.append(f"Поручений: {len(tasks)}" + (f" ({', '.join(roles)})" if roles else "") + ".")
    if an.get("urgency") == "high":
        parts.append("Срочно.")
    return doc.title, " ".join(p for p in parts if p).strip()


def _apply_letter(env: Env, a: AIAction) -> dict:
    from bis.services.correspondence import process_incoming

    doc = _ensure_document(env, _event(env, a))
    text = parse_file(env.storage.get(doc.file_key), doc.file_name or "letter.txt", doc.mime).text
    analysis = process_incoming(env.db, env.storage, env.gateway, doc, actor="agent:secretary", text=text)
    a.title, a.summary = letter_outcome(doc)
    return {"document_id": str(doc.id), "number": doc.number, "category": analysis.category,
            "tasks": len((doc.data or {}).get("tasks", []))}


def _apply_contract(env: Env, a: AIAction) -> dict:
    from bis.services import contracts as csvc

    doc = _ensure_document(env, _event(env, a))
    text = parse_file(env.storage.get(doc.file_key), doc.file_name or "contract.txt", doc.mime).text
    c = Contract(organization_id=env.org_id, kind=a.payload["kind"], our_role=a.payload["our_role"], number="б/н",
                 object_id=_uuid(a.payload.get("object_id")), document_id=doc.id)
    env.db.add(c)
    env.db.flush()
    doc.contract_id = c.id
    extracted, _, added = csvc.analyze_contract(env.db, env.storage, env.gateway, c, actor="agent:lawyer", text=text)
    if extracted.number:
        c.number = extracted.number[:100]
    doc.title = f"Договор № {c.number}" + (f" от {c.date:%d.%m.%Y}" if c.date else "")
    a.title = f"{doc.title}: обязательства на контроле"
    a.summary = (f"Обязательств поставлено на контроль: {added}." + (f" Сумма {c.amount:,.0f} руб.".replace(",", " ") if c.amount else "")
                 + (f" Срок: {c.end_date:%d.%m.%Y}." if c.end_date else "")
                 + (f" Риски: {'; '.join(extracted.risks[:3])}." if extracted.risks else ""))
    return {"contract_id": str(c.id), "obligations_added": added, "risks": extracted.risks[:5]}


APPLY: dict[str, Callable[[Env, AIAction], dict]] = {
    "register_document": _apply_register, "create_receipt": _apply_receipt, "report_volume": _apply_volume,
    "record_usage": _apply_usage, "link_certificate": _apply_certificate, "create_task": _apply_task,
    "flag_issue": _apply_flag, "info": lambda env, a: {}, "draft_aosr": _apply_aosr, "build_scheme": _apply_scheme,
    "process_letter": _apply_letter, "import_contract": _apply_contract,
}


def apply_action(env: Env, a: AIAction, *, auto: bool) -> None:
    try:
        with env.db.begin_nested():
            a.result = _json(APPLY[a.kind](env, a))
        a.status, a.auto = "applied", auto
    except Exception as e:  # noqa: BLE001
        log.warning("Действие ИИ %s не выполнено: %s", a.kind, e)
        a.status, a.result = "failed", {"error": str(e)[:1000]}
    audit(env.db, env.org_id, actor_type="agent", actor=a.specialist, action=f"ai.{a.kind}", entity_type="ai_action",
          entity_id=a.id, details={"auto": auto, "status": a.status, "title": a.title[:200]})


def _undo(env: Env, a: AIAction) -> None:
    r = a.result or {}
    for key, model in (("receipt_id", MaterialReceipt), ("volume_id", WorkVolume), ("usage_id", MaterialUsage)):
        row = env.db.get(model, uuid.UUID(r[key])) if r.get(key) else None
        if row is not None:
            if model is WorkVolume and (row.status == "closed" or row.ks2_document_id):
                raise ValueError("Объём уже закрыт в КС-2 — откат невозможен")
            env.db.delete(row)
            env.db.flush()
    if r.get("material_created") and r.get("material_id"):
        mat = env.db.get(Material, uuid.UUID(r["material_id"]))
        used = env.db.scalar(select(func.count()).select_from(MaterialReceipt).where(MaterialReceipt.material_id == mat.id))
        if mat and not used:
            env.db.delete(mat)
    if a.kind == "link_certificate":
        for rid in r.get("receipt_ids", []):
            rec = env.db.get(MaterialReceipt, uuid.UUID(rid))
            if rec and rec.quality_doc == a.payload.get("text", "")[:500]:
                rec.quality_doc = None
    if r.get("task_id"):
        t = env.db.get(Task, uuid.UUID(r["task_id"]))
        if t and t.status in ("open", "in_progress"):
            t.status = "cancelled"
    if a.kind == "process_letter" and r.get("document_id"):  # поручения по письму больше не нужны
        letter = env.db.get(Document, uuid.UUID(r["document_id"]))
        for tid in ((letter.data or {}).get("tasks") or []) if letter else []:
            t = env.db.get(Task, uuid.UUID(tid))
            if t and t.status in ("open", "in_progress"):
                t.status = "cancelled"
    if a.kind == "import_contract" and r.get("contract_id"):
        c = env.db.get(Contract, uuid.UUID(r["contract_id"]))
        if c:
            for d in env.db.scalars(select(Document).where(Document.contract_id == c.id)):
                d.contract_id = None
            env.db.flush()
            env.db.delete(c)
    if a.kind in ("draft_aosr", "build_scheme", "register_document") and r.get("document_id"):
        doc = env.db.get(Document, uuid.UUID(r["document_id"]))
        if doc and doc.status in ("draft", "registered"):
            doc.status, doc.reject_reason = "rejected", "Отклонено в ленте ИИ"
            env.db.execute(delete(KnowledgeSource).where(KnowledgeSource.document_id == doc.id))
            for v in env.db.scalars(select(WorkVolume).where(WorkVolume.aosr_document_id == doc.id)):
                v.aosr_document_id = None
        elif doc:
            raise ValueError("Документ уже утверждён — откат невозможен")
    env.db.flush()


def learn(env: Env, a: AIAction, changes: dict[str, Any]) -> None:
    """Исправление человека → пример для сопоставления в следующий раз."""
    if "material_id" in changes and a.payload.get("source_name"):
        remember_alias(env, "materials", a.payload["source_name"], changes["material_id"])
    if "estimate_item_id" in changes and a.payload.get("source_text"):
        remember_alias(env, "works", a.payload["source_text"], changes["estimate_item_id"])


def review(env: Env, a: AIAction, *, approve: bool, user: User, reason: str | None = None,
           changes: dict[str, Any] | None = None) -> AIAction:
    """Решение человека по действию ИИ: утвердить (с правками) или отклонить (с откатом)."""
    if a.reviewed_at is not None or a.status == "rejected":
        raise ValueError("Действие уже рассмотрено")
    env.created = []
    env.user = env.user or user
    changes = {k: v for k, v in (changes or {}).items() if k in EDITABLE.get(a.kind, ())}
    if approve:
        if changes:
            if a.status == "applied":
                _undo(env, a)
            learn(env, a, changes)
            payload = {**a.payload, **_json(changes)}
            if "material_id" in changes:
                payload["new_material"] = None
            a.payload, a.corrected, a.status = payload, True, "proposed"
            if "object_id" in changes:
                a.object_id = _uuid(changes["object_id"])
        if a.status in ("proposed", "failed"):
            apply_action(env, a, auto=False)
            verify(env, _event(env, a), [a])
    else:
        if a.status == "applied":
            _undo(env, a)
        a.status = "rejected"
        if reason and reason.strip():
            spec = BY_NAME.get(a.specialist)
            for scope in [a.specialist] + ([spec.agent] if spec and spec.agent else []):
                env.db.add(OrgMemory(organization_id=env.org_id, kind="feedback", scope=scope,
                                     content=f"{ACTION_TITLES.get(a.kind, a.kind)} «{a.title[:200]}» отклонено: "
                                             f"{reason.strip()[:500]}", source=f"ai_action:{a.id}"))
    a.reviewed_by_id, a.reviewed_at, a.reason = user.id, dt.datetime.now(dt.UTC), reason
    audit(env.db, env.org_id, actor_type="user", actor=str(user.id), action="ai.review", entity_type="ai_action",
          entity_id=a.id, details={"approve": approve, "reason": reason, "changes": _json(changes)})
    _decide_all(env)
    env.db.flush()
    return a


def autonomy_suggestions(db: Session, org_id: uuid.UUID, autonomy: dict[str, str], days: int = 90) -> list[dict[str, Any]]:
    """Где ИИ уже заслужил самостоятельность, а где ошибается слишком часто."""
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    rows = db.execute(select(AIAction.kind, AIAction.status, AIAction.corrected, func.count())
                      .where(AIAction.organization_id == org_id, AIAction.reviewed_at.is_not(None), AIAction.reviewed_at >= since)
                      .group_by(AIAction.kind, AIAction.status, AIAction.corrected)).all()
    agg: dict[str, dict[str, int]] = {}
    for kind, status, corrected, n in rows:
        st = agg.setdefault(kind, {"ok": 0, "corrected": 0, "rejected": 0})
        st["rejected" if status == "rejected" else "corrected" if corrected else "ok"] += n
    out = []
    for kind, st in agg.items():
        n = sum(st.values())
        mode = autonomy.get(kind, "propose")
        bad = st["rejected"] + st["corrected"]
        if mode == "propose" and n >= 10 and st["ok"] / n >= 0.9:
            out.append({"kind": kind, "title": ACTION_TITLES.get(kind, kind), "suggest": "auto", "decisions": n,
                        "reason": f"{st['ok']} из {n} предложений утверждены без правок — можно выполнять сразу"})
        elif mode == "auto" and n >= 5 and bad / n > 0.2:
            out.append({"kind": kind, "title": ACTION_TITLES.get(kind, kind), "suggest": "propose", "decisions": n,
                        "reason": f"{bad} из {n} отклонены или исправлены — лучше спрашивать"})
    return out
