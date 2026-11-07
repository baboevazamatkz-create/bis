"""Telegram: прорабы, мастера и снабженцы шлют с площадки фото, голосовые, документы и текст — ИИ разбирает и отвечает.

- Привязка: в веб-интерфейсе пользователь получает код (POST /api/telegram/link) и отправляет боту «/start <код>».
- Объект для сообщений: «/объект ЖК-01» — иначе определяется по тексту, по назначению приказом или единственный активный.
- На предложения ИИ (приход материалов, договор) бот присылает кнопки «Принять / Отклонить» — решение прямо в чате.
- Режимы: webhook (POST /api/telegram/webhook, секрет в заголовке X-Telegram-Bot-Api-Secret-Token) или опрос
  (bis telegram-poll) — для закрытого контура без входящих соединений.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import logging
import struct
import time
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.ai import conveyor as cv
from bis.config import get_settings
from bis.models import AIAction, ConstructionObject, IntakeEvent, Organization, User
from bis.security import has_scope

log = logging.getLogger(__name__)
MAX_FILE = 20 * 1024 * 1024  # Bot API отдаёт ботам файлы до 20 МБ
LINK_TTL = 15 * 60
HELP = ("Пришлите с объекта фото, голосовое, документ (УПД, сертификат, протокол, письмо, съёмку) или текст — "
        "ИИ разберёт: внесёт объёмы и расход, приход материалов, привяжет сертификаты, соберёт АОСР, поставит поручения.\n"
        "Команды: /объект <шифр> — объект для следующих сообщений; /ждут — что ждёт вашего решения; /помощь.")


class TelegramError(Exception):
    pass


class TelegramAPI:
    def __init__(self, token: str, base: str = "https://api.telegram.org", client: httpx.Client | None = None):
        self.token, self.base = token, base.rstrip("/")
        self.client = client or httpx.Client(timeout=70)

    def call(self, method: str, **params: Any) -> Any:
        r = self.client.post(f"{self.base}/bot{self.token}/{method}", json={k: v for k, v in params.items() if v is not None})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description') or r.status_code}")
        return data["result"]

    def download(self, file_id: str) -> bytes:
        path = self.call("getFile", file_id=file_id)["file_path"]
        r = self.client.get(f"{self.base}/file/bot{self.token}/{path}")
        if r.status_code != 200:
            raise TelegramError(f"Файл не получен: {r.status_code}")
        return r.content

    def send(self, chat_id: str | int, text: str, *, reply_to: int | None = None,
             buttons: list[list[dict[str, str]]] | None = None) -> Any:
        return self.call("sendMessage", chat_id=chat_id, text=text[:4000],
                         reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None,
                         reply_markup={"inline_keyboard": buttons} if buttons else None)


_api: TelegramAPI | None = None


def get_api() -> TelegramAPI | None:
    global _api
    s = get_settings()
    if _api is None and s.telegram_bot_token:
        _api = TelegramAPI(s.telegram_bot_token, s.telegram_api_base)
    return _api


def set_api(api: TelegramAPI | None) -> None:
    global _api
    _api = api


# --- привязка аккаунта ----------------------------------------------------------------------------


def _sign(payload: bytes) -> bytes:
    return hmac.new(get_settings().secret_key.encode(), b"tg-link:" + payload, hashlib.sha256).digest()[:9]


def link_code(user_id: uuid.UUID, now: float | None = None) -> str:
    """Одноразовый по сроку код для «/start <код>» (до 64 символов из [A-Za-z0-9_-])."""
    payload = user_id.bytes + struct.pack(">I", int((now or time.time()) + LINK_TTL))
    return base64.urlsafe_b64encode(payload + _sign(payload)).decode().rstrip("=")


def parse_link_code(code: str, now: float | None = None) -> uuid.UUID | None:
    try:
        raw = base64.urlsafe_b64decode(code + "=" * (-len(code) % 4))
    except (ValueError, TypeError):
        return None
    payload, sig = raw[:20], raw[20:]
    if len(raw) != 29 or not hmac.compare_digest(sig, _sign(payload)):
        return None
    if struct.unpack(">I", payload[16:])[0] < (now or time.time()):
        return None
    return uuid.UUID(bytes=payload[:16])


# --- обработка обновлений ---------------------------------------------------------------------------


def _chat_object(org: Organization, chat_id: str) -> uuid.UUID | None:
    oid = ((org.settings or {}).get("telegram") or {}).get("objects", {}).get(chat_id)
    return uuid.UUID(oid) if oid else None


def _set_chat_object(org: Organization, chat_id: str, object_id: uuid.UUID | None) -> None:
    tg = dict((org.settings or {}).get("telegram") or {})
    objects = dict(tg.get("objects") or {})
    if object_id:
        objects[chat_id] = str(object_id)
    else:
        objects.pop(chat_id, None)
    org.settings = {**(org.settings or {}), "telegram": {**tg, "objects": objects}}


def _buttons(actions: list[AIAction]) -> list[list[dict[str, str]]]:
    rows = []
    for a in [x for x in actions if x.status == "proposed"][:5]:
        short = a.title if len(a.title) <= 40 else a.title[:39] + "…"
        rows.append([{"text": f"✓ {short}", "callback_data": f"ok:{a.id}"},
                     {"text": "✗", "callback_data": f"no:{a.id}"}])
    return rows


def _file_of(msg: dict[str, Any]) -> tuple[str, str, str, int] | None:
    """(file_id, имя, mime, размер) из сообщения Telegram."""
    mid = msg.get("message_id", 0)
    if msg.get("photo"):
        p = max(msg["photo"], key=lambda x: (x.get("width", 0) * x.get("height", 0), x.get("file_size") or 0))  # самое крупное
        return p["file_id"], f"photo_{mid}.jpg", "image/jpeg", p.get("file_size") or 0
    for key, ext, mime in (("voice", "ogg", "audio/ogg"), ("audio", "mp3", "audio/mpeg"), ("video_note", "mp4", "video/mp4")):
        if msg.get(key):
            f = msg[key]
            return f["file_id"], f.get("file_name") or f"{key}_{mid}.{ext}", f.get("mime_type") or mime, f.get("file_size") or 0
    if msg.get("document"):
        d = msg["document"]
        return d["file_id"], d.get("file_name") or f"document_{mid}", d.get("mime_type") or "application/octet-stream", \
            d.get("file_size") or 0
    return None


def handle_update(db: Session, api: TelegramAPI, update: dict[str, Any], *, env_factory=None) -> str:
    """Обрабатывает одно обновление Telegram. Возвращает краткий итог (для журнала и тестов)."""
    if update.get("callback_query"):
        return _handle_callback(db, api, update["callback_query"], env_factory)
    msg = update.get("message")
    if not msg or msg.get("chat", {}).get("type") not in (None, "private", "group", "supergroup"):
        return "skip"
    chat_id, mid = str(msg["chat"]["id"]), msg.get("message_id")
    sender_id = str((msg.get("from") or {}).get("id") or chat_id)  # в группе пользователь — отправитель, а не чат
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if text.startswith("/") and "@" in text.split()[0]:  # «/объект@bis_bot ЖК-01» в группах
        head, _, rest = text.partition(" ")
        text = (head.split("@")[0] + " " + rest).strip()

    if text.startswith("/start"):
        code = text.split(maxsplit=1)[1].strip() if " " in text else ""
        user_id = parse_link_code(code) if code else None
        user = db.get(User, user_id) if user_id else None
        if user is None or not user.is_active:
            api.send(chat_id, "Код не подошёл или устарел. Получите новый в веб-интерфейсе: профиль → «Подключить Telegram».")
            return "link:bad"
        for other in db.scalars(select(User).where(User.telegram_chat_id == sender_id, User.id != user.id)):
            other.telegram_chat_id = None
        user.telegram_chat_id = sender_id
        db.commit()
        api.send(chat_id, f"Готово, {user.full_name}: бот привязан к вашему аккаунту.\n\n{HELP}")
        return "link:ok"

    user = db.scalar(select(User).where(User.telegram_chat_id == sender_id, User.is_active.is_(True)))
    if user is None:
        api.send(chat_id, "Бот не привязан к аккаунту. В веб-интерфейсе откройте профиль → «Подключить Telegram» "
                          "и отправьте сюда полученную команду /start.")
        return "unknown"
    org = db.get(Organization, user.organization_id)
    low = text.lower()

    if low in ("/help", "/помощь"):
        api.send(chat_id, HELP)
        return "help"
    if low.startswith(("/объект", "/object")):
        arg = text.split(maxsplit=1)[1].strip() if " " in text else ""
        objs = db.scalars(select(ConstructionObject).where(ConstructionObject.organization_id == org.id,
                                                           ConstructionObject.status == "active")).all()
        found = [o for o in objs if arg and (arg.lower() == (o.code or "").lower() or arg.lower() in o.name.lower())]
        if len(found) == 1:
            _set_chat_object(org, chat_id, found[0].id)
            db.commit()
            api.send(chat_id, f"Объект для сообщений: {found[0].code or ''} {found[0].name}".strip())
            return "object:set"
        listing = "\n".join(f"• {o.code or '—'} — {o.name}" for o in objs[:20])
        api.send(chat_id, ("Уточните объект" if arg else "Укажите шифр объекта") + f": /объект <шифр>\n{listing}")
        return "object:list"
    if low in ("/ждут", "/pending"):
        pending = db.scalars(select(AIAction).where(AIAction.organization_id == org.id, AIAction.status == "proposed")
                             .order_by(AIAction.created_at.desc()).limit(5)).all()
        if not pending:
            api.send(chat_id, "Решений не ждёт ничего — ИИ справился сам.")
        else:
            api.send(chat_id, "Ждут решения:\n" + "\n".join(f"• {a.title}" for a in pending),
                     buttons=_buttons(list(pending)) if has_scope(user.role, "documents:write") else None)
        return "pending"

    update_id = update.get("update_id")
    if update_id is not None and db.scalar(select(IntakeEvent.id).where(
            IntakeEvent.source == "telegram", IntakeEvent.result["telegram"]["update_id"].astext == str(update_id))):
        return "duplicate"
    data = file_name = mime = None
    info = _file_of(msg)
    if info:
        file_id, file_name, mime, size = info
        if size > MAX_FILE:
            api.send(chat_id, "Файл больше 20 МБ — Telegram не отдаёт ботам такие файлы. Загрузите его через веб-интерфейс.",
                     reply_to=mid)
            return "too_big"
        data = api.download(file_id)
    elif not text:
        return "empty"

    env = (env_factory or _default_env)(db, user)
    event = cv.ingest(env, source="telegram", text=text or None, data=data, file_name=file_name, mime=mime,
                      object_id=_chat_object(org, chat_id), sender=user.full_name)
    event.result = {"telegram": {"chat_id": chat_id, "message_id": mid, "update_id": update_id}}
    actions = cv.process(env, event)
    db.commit()
    reply = (event.result or {}).get("reply") or "Принято."
    if event.object_id is None:
        reply += "\nОбъект не определён — укажите: /объект <шифр>."
    s = get_settings()
    if s.public_url and any(a.status == "proposed" for a in actions):
        reply += f"\nЛента ИИ: {s.public_url.rstrip('/')}/#feed"
    can_review = has_scope(user.role, "documents:write")
    api.send(chat_id, reply, reply_to=mid, buttons=_buttons(actions) if can_review else None)
    return f"event:{event.status}"


def _default_env(db: Session, user: User) -> cv.Env:
    from bis.llm.gateway import get_gateway
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    return cv.Env(db=db, storage=get_storage(), gateway=get_gateway(), embedder=get_embedder(),
                  org_id=user.organization_id, user=user)


def _handle_callback(db: Session, api: TelegramAPI, cq: dict[str, Any], env_factory) -> str:
    sender_id = str((cq.get("from") or {}).get("id"))
    chat_id = str((cq.get("message") or {}).get("chat", {}).get("id") or sender_id)
    user = db.scalar(select(User).where(User.telegram_chat_id == sender_id, User.is_active.is_(True)))
    op, _, raw_id = (cq.get("data") or "").partition(":")
    try:
        action = db.get(AIAction, uuid.UUID(raw_id)) if op in ("ok", "no") else None
    except ValueError:
        action = None
    if user is None or action is None or action.organization_id != user.organization_id:
        api.call("answerCallbackQuery", callback_query_id=cq["id"], text="Действие не найдено")
        return "callback:unknown"
    if not has_scope(user.role, "documents:write"):
        api.call("answerCallbackQuery", callback_query_id=cq["id"], text="Недостаточно прав")
        return "callback:forbidden"
    env = (env_factory or _default_env)(db, user)
    try:
        cv.review(env, action, approve=op == "ok", user=user)
        db.commit()
    except ValueError as e:
        db.rollback()
        api.call("answerCallbackQuery", callback_query_id=cq["id"], text=str(e)[:190])
        return "callback:error"
    verdict = {"applied": "✓ Выполнено", "rejected": "✗ Отклонено", "failed": "Не удалось выполнить"}.get(action.status,
                                                                                                        action.status)
    api.call("answerCallbackQuery", callback_query_id=cq["id"], text=verdict)
    extra = [f"⚠ {x.title}" for x in env.created if x.kind == "flag_issue" and x.severity != "info"]
    api.send(chat_id, f"{verdict}: {action.title}" + (f" — {action.result.get('error')}" if action.status == "failed" else "")
             + ("\n" + "\n".join(extra) if extra else ""))
    return f"callback:{action.status}"


def handle_update_safely(update: dict[str, Any]) -> None:
    """Для webhook и опроса: своя сессия БД, ошибки — в журнал, а не наружу."""
    from bis.db import SessionLocal

    api = get_api()
    if api is None:
        return
    db = SessionLocal()
    try:
        handle_update(db, api, update)
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("Telegram: обновление %s", update.get("update_id"))
        msg = update.get("message") or {}
        if msg.get("chat"):
            with contextlib.suppress(TelegramError, httpx.HTTPError):
                api.send(msg["chat"]["id"], "Не удалось обработать сообщение — попробуйте ещё раз или загрузите через веб.")
    finally:
        db.close()


def poll(stop_after: int | None = None) -> None:
    """Долгий опрос getUpdates — когда до сервера нельзя достучаться снаружи (закрытый контур)."""
    api = get_api()
    if api is None:
        raise RuntimeError("Не задан BIS_TELEGRAM_BOT_TOKEN")
    api.call("deleteWebhook")
    offset, handled = None, 0
    while stop_after is None or handled < stop_after:
        try:
            updates = api.call("getUpdates", offset=offset, timeout=50,
                               allowed_updates=["message", "callback_query"])
        except (TelegramError, httpx.HTTPError) as e:
            log.warning("Telegram: %s", e)
            time.sleep(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            handle_update_safely(upd)
            handled += 1
