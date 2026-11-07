"""Telegram-бот: привязка, приём сообщений и файлов с площадки, решения кнопками, webhook."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from bis.config import get_settings
from bis.connectors import telegram as tg
from bis.models import AIAction, IntakeEvent, MaterialReceipt, Organization, User

UPD = """Универсальный передаточный документ
Счет-фактура № 301 от 01.06.2026
Продавец: ООО «БетонСтрой» ИНН 7705123456
1 | Бетон B25 W8 F150 | м3 | 40 | 7 250,00 | 290 000,00"""
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


class FakeBot:
    """Имитация Bot API: файлы по file_id, журнал отправленных сообщений и ответов на кнопки."""

    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = files or {}
        self.sent: list[dict] = []
        self.answers: list[dict] = []
        self.documents: list[bytes] = []
        self.actions: list[str] = []
        self.api = tg.TelegramAPI("T", "https://tg.test", client=httpx.Client(transport=httpx.MockTransport(self.handle)))

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/file/botT/"):
            return httpx.Response(200, content=self.files[path.rsplit("/", 1)[-1]])
        method = path.rsplit("/", 1)[-1]
        if method == "sendDocument":  # multipart: файл
            self.documents.append(request.content)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 2}})
        body = json.loads(request.content or b"{}")
        if method == "sendChatAction":
            self.actions.append(body["action"])
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": f"docs/{body['file_id']}"}})
        if method == "sendMessage":
            self.sent.append(body)
        elif method == "answerCallbackQuery":
            self.answers.append(body)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    @property
    def last(self) -> str:
        return self.sent[-1]["text"]


def msg(update_id: int, chat: int = 555, **fields) -> dict:
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": chat, "type": "private"},
                                                "from": {"id": chat}, **fields}}


def link(db, bot, email="admin@demo.bis", chat=555) -> User:
    user = db.scalar(select(User).where(User.email == email))
    assert tg.handle_update(db, bot.api, msg(1, chat, text=f"/start {tg.link_code(user.id)}")) == "link:ok"
    return user


def test_link_code_roundtrip():
    import uuid

    uid = uuid.uuid4()
    code = tg.link_code(uid, now=1000)
    assert len(code) <= 64 and tg.parse_link_code(code, now=1000) == uid
    assert tg.parse_link_code(code, now=1000 + tg.LINK_TTL + 1) is None  # устарел
    assert tg.parse_link_code(code[:-2] + ("AA" if code[-2:] != "AA" else "BB"), now=1000) is None  # подделан
    assert tg.parse_link_code("мусор", now=1000) is None


def test_unlinked_chat_and_linking(db, demo):
    bot = FakeBot()
    assert tg.handle_update(db, bot.api, msg(1, 777, text="Залили 10 м3")) == "unknown"
    assert "не привязан" in bot.last
    assert tg.handle_update(db, bot.api, msg(2, 777, text="/start плохой-код")) == "link:bad"
    user = link(db, bot, chat=777)
    assert db.get(User, user.id).telegram_chat_id == "777" and "привязан" in bot.last


def test_foreman_text_is_processed_and_answered(db, demo):
    bot = FakeBot()
    link(db, bot)
    r = tg.handle_update(db, bot.api, msg(10, text="Залили бетон плиты Пм1 захватка 3 — 310 м3. Не привезли мембрану, "
                                                   "бригада простаивает."))
    assert r == "event:done"
    ev = db.scalar(select(IntakeEvent).where(IntakeEvent.source == "telegram"))
    assert ev.result["telegram"]["message_id"] == 10 and ev.sender == "Иванов Иван Иванович"
    reply = bot.sent[-1]
    assert "Понял" in reply["text"] and "✓" in reply["text"] and "reply_markup" not in reply  # всё сделано сам
    assert tg.handle_update(db, bot.api, msg(10, text="повтор")) == "duplicate"


def test_document_proposal_and_button_decision(db, demo):
    bot = FakeBot(files={"upd1": UPD.encode()})
    link(db, bot)
    upd = msg(20, document={"file_id": "upd1", "file_name": "УПД_301.txt", "mime_type": "text/plain", "file_size": 300})
    assert tg.handle_update(db, bot.api, upd) == "event:done"
    buttons = bot.sent[-1]["reply_markup"]["inline_keyboard"]
    assert buttons and buttons[0][0]["callback_data"].startswith("ok:")  # приход — решение кнопкой
    action_id = buttons[0][0]["callback_data"][3:]
    cq = {"update_id": 21, "callback_query": {"id": "cq1", "data": f"ok:{action_id}", "from": {"id": 555},
                                              "message": {"chat": {"id": 555}, "message_id": 20}}}
    assert tg.handle_update(db, bot.api, cq) == "callback:applied"
    a = db.get(AIAction, action_id)
    assert a.status == "applied" and db.get(MaterialReceipt, a.result["receipt_id"]).quantity == 40
    assert bot.answers[-1]["text"].startswith("✓") and "Выполнено" in bot.last
    assert tg.handle_update(db, bot.api, {**cq, "update_id": 22}) == "callback:error"  # повторное нажатие


def test_photo_and_object_command(db, demo):
    bot = FakeBot(files={"ph1": PNG})
    link(db, bot)
    assert tg.handle_update(db, bot.api, msg(30, text="/объект ЖК-01")) == "object:set"
    org = db.get(Organization, demo.id)
    obj_id = org.settings["telegram"]["objects"]["555"]
    photo = msg(31, caption="захватка 3", photo=[{"file_id": "ph0", "width": 90, "height": 90},
                                                 {"file_id": "ph1", "width": 1280, "height": 960, "file_size": 70}])
    assert tg.handle_update(db, bot.api, photo) == "event:done"
    ev = db.scalar(select(IntakeEvent).where(IntakeEvent.kind == "photo"))
    assert str(ev.object_id) == obj_id and ev.file_name == "photo_31.jpg" and ev.mime == "image/jpeg"
    big = msg(32, document={"file_id": "x", "file_name": "big.pdf", "file_size": 30 * 1024 * 1024})
    assert tg.handle_update(db, bot.api, big) == "too_big"
    assert tg.handle_update(db, bot.api, msg(33, text="/ждут")) == "pending"


def test_webhook_requires_secret(client, demo, monkeypatch):
    s = get_settings()
    assert client.post("/api/telegram/webhook", json={}).status_code == 403  # не настроен
    monkeypatch.setattr(s, "telegram_bot_token", "T")
    monkeypatch.setattr(s, "telegram_webhook_secret", "s3cret")
    bot = FakeBot()
    tg.set_api(bot.api)
    try:
        assert client.post("/api/telegram/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "x"}).status_code == 403
        r = client.post("/api/telegram/webhook", json=msg(40, 999, text="привет"),
                        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})
        assert r.status_code == 200 and "не привязан" in bot.last  # обработано в фоне
    finally:
        tg.set_api(None)


@pytest.fixture
def tg_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "telegram_bot_token", "T")
    monkeypatch.setattr(get_settings(), "telegram_bot_username", "bis_demo_bot")


def test_link_endpoint(client, auth, tg_token):
    r = client.post("/api/telegram/link", headers=auth).json()
    assert r["command"].startswith("/start ") and r["deep_link"].startswith("https://t.me/bis_demo_bot?start=")
    assert tg.parse_link_code(r["code"]) is not None


def test_question_is_answered_with_documents(db, demo, monkeypatch):
    """Вопрос прораба — не отчёт: ИИ отвечает и присылает найденные документы файлами (как TrunkText)."""
    from bis.agents.base import AgentResult
    from bis.models import ChatMessage, Document
    from bis.plugins.sdk import get_registry

    bot = FakeBot()
    link(db, bot)
    doc = db.scalars(select(Document).where(Document.organization_id == demo.id, Document.file_key.is_not(None))).first()
    seen = {}

    def fake_run(self, ctx, registry, history, text):
        seen["question"], seen["agent"] = text, self.name
        return AgentResult(text=f"Ответ по документу [{doc.title}](document:{doc.id}) **важно**", agent=self.name,
                           documents=[{"id": str(doc.id), "title": doc.title}])

    reg = get_registry()
    monkeypatch.setattr(type(next(iter(reg.agents.values()))), "run", fake_run)
    r = tg.handle_update(db, bot.api, msg(20, text="Какая марка бетона на плиту Пм1?"))
    assert r.startswith("answer:") and r.endswith(":1")
    assert seen["question"] == "Какая марка бетона на плиту Пм1?" and "typing" in bot.actions
    assert bot.last == f"Ответ по документу {doc.title} важно" and len(bot.documents) == 1
    assert not db.scalar(select(IntakeEvent).where(IntakeEvent.source == "telegram"))  # не ушло в разбор как отчёт
    assert db.scalar(select(ChatMessage).where(ChatMessage.content == "Какая марка бетона на плиту Пм1?"))
    tg.handle_update(db, bot.api, msg(21, text="/вопрос пришли АОСР на армирование"))
    assert seen["question"] == "пришли АОСР на армирование"
    assert tg.handle_update(db, bot.api, msg(22, text="Залили бетон плиты Пм1 — 300 м3")).startswith("event:")
