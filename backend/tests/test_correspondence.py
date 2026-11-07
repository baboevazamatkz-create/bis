"""Входящая корреспонденция: разбор письма, поручения по ролям, приём почты по IMAP."""

from __future__ import annotations

import datetime as dt
from email.message import EmailMessage

from bis.services.correspondence import add_business_days, heuristic_incoming

LETTER = """АО «Заказчик-Девелопмент»
ИНН 7702000003
Исх. № 145/ТН от 02.11.2026
О предоставлении исполнительной документации
В соответствии с договором генподряда № ГП-01/2026 просим в течение 5 рабочих дней предоставить исполнительную документацию на армирование фундаментной плиты.
Также просим направить сертификаты на арматуру А500С до 10.11.2026.
Технический директор Петров П.П."""


def test_heuristic_incoming():
    a = heuristic_incoming(LETTER, received=dt.date(2026, 11, 3))
    assert a.sender_org == "АО «Заказчик-Девелопмент»" and a.sender_inn == "7702000003"
    assert a.their_number == "145/ТН" and a.their_date == dt.date(2026, 11, 2)
    assert a.subject == "О предоставлении исполнительной документации"
    assert a.category == "request" and a.response_required and a.contract_number == "ГП-01/2026"
    assert a.response_due == dt.date(2026, 11, 9)  # 5 рабочих дней от 02.11 (пн) → 09.11
    assert [(r.role, r.due_date) for r in a.requests] == [("pto", dt.date(2026, 11, 9)), ("supply", dt.date(2026, 11, 10))]


def test_claim_is_urgent():
    a = heuristic_incoming("ООО «Ромашка»\nПРЕТЕНЗИЯ\nТребуем уплатить неустойку в течение 10 дней.", received=dt.date(2026, 1, 12))
    assert a.category == "claim" and a.urgency == "high" and a.response_due == dt.date(2026, 1, 22)
    assert a.requests[0].role == "lawyer"


def test_business_days():
    assert add_business_days(dt.date(2026, 11, 6), 1) == dt.date(2026, 11, 9)  # пятница → понедельник


def _upload_letter(client, auth, text=LETTER):
    r = client.post("/api/documents/upload", headers=auth, data={"kind": "incoming_letter"},
                    files={"file": ("letter.txt", text.encode(), "text/plain")})
    assert r.status_code == 201, r.text
    return r.json()


def test_incoming_upload_creates_tasks_for_roles(client, auth):
    supply = client.post("/api/users", headers=auth, json={"email": "supply@demo.bis", "full_name": "Снабженец Семён",
                                                           "role": "supply", "password": "supply123"})
    assert supply.status_code == 201
    doc = _upload_letter(client, auth)
    assert doc["number"].startswith("вх-") and doc["status"] == "registered"
    assert "запрос" in doc["title"] and doc["contract_id"] is not None
    analysis = doc["data"]["analysis"]
    assert analysis["their_number"] == "145/ТН"
    tasks = client.get("/api/tasks", headers=auth).json()
    by_title = {t["title"][:30]: t for t in tasks}
    assert len(tasks) == 3  # два требования + подготовка ответа
    pto_user = next(u for u in client.get("/api/users", headers=auth).json() if u["role"] == "pto")
    pto_task = next(t for t in tasks if "исполнительную документацию" in t["title"])
    assert pto_task["assignee_user_id"] == pto_user["id"] and pto_task["due_date"] == "2026-11-09"
    supply_task = next(t for t in tasks if "сертификаты" in t["title"])
    assert supply_task["assignee_user_id"] == supply.json()["id"]
    assert any(t.startswith("Подготовить ответ") for t in by_title)
    # исполнителю пришло уведомление
    from conftest import login

    pto = login(client, "pto@demo.bis", "demo12345")
    assert any("Поручение по вх." in n["title"] for n in client.get("/api/notifications", headers=pto).json())


def test_routing_rules_override(client, auth):
    org = client.get("/api/org", headers=auth).json()
    settings = {**org["settings"], "routing": {"pto": "manager@demo.bis"}}
    client.patch("/api/org", headers=auth, json={"settings": settings})
    _upload_letter(client, auth)
    manager = next(u for u in client.get("/api/users", headers=auth).json() if u["email"] == "manager@demo.bis")
    task = next(t for t in client.get("/api/tasks", headers=auth).json() if "исполнительную" in t["title"])
    assert task["assignee_user_id"] == manager["id"]


class FakeImap:
    def __init__(self, messages: dict[bytes, bytes]):
        self.messages = messages
        self.seen: list[bytes] = []

    def select(self, folder):
        return "OK", [b"1"]

    def search(self, charset, criterion):
        return "OK", [b" ".join(k for k in self.messages if k not in self.seen)]

    def fetch(self, msg_id, parts):
        return "OK", [(b"1 (RFC822)", self.messages[msg_id]), b")"]

    def store(self, msg_id, flags, value):
        self.seen.append(msg_id)

    def logout(self):
        pass


def _eml(message_id: str, body: str, attachment: bytes | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = "АО «Заказчик-Девелопмент» <office@developer.example>"
    msg["To"] = "info@stroydemo.example"
    msg["Subject"] = "О предоставлении исполнительной документации"
    msg["Message-ID"] = message_id
    msg.set_content(body)
    if attachment:
        msg.add_attachment(attachment, maintype="text", subtype="plain", filename="Перечень.txt")
    return msg.as_bytes()


def test_mailbox_import_is_idempotent(client, auth, db):
    from bis.connectors.mailbox import fetch_mailbox
    from bis.models import Document, Organization

    org = db.query(Organization).filter_by(name="ООО «СтройДемо»").one()
    org.settings = {**org.settings, "mailbox": {"enabled": True, "host": "imap.test", "username": "u"}}
    db.commit()
    raw = _eml("<m1@test>", LETTER, attachment="Перечень: АОСР № 1-5, схемы".encode())
    imap = FakeImap({b"1": raw})
    assert fetch_mailbox(db, org, imap_factory=lambda cfg: imap) == {"imported": 1, "skipped": 0}
    assert imap.seen == [b"1"]
    letters = db.query(Document).filter_by(kind="incoming_letter", source="email").all()
    assert len(letters) == 1 and letters[0].data["analysis"]["category"] == "request"
    assert db.query(Document).filter(Document.title.like("Вложение:%")).count() == 1
    # то же письмо повторно (например, снова помечено непрочитанным) — не дублируется
    imap2 = FakeImap({b"7": raw})
    assert fetch_mailbox(db, org, imap_factory=lambda cfg: imap2) == {"imported": 0, "skipped": 1}
    res = client.get("/api/knowledge/search", headers=auth, params={"q": "перечень АОСР схемы", "scope": "organization"})
    assert any("Перечень" in r["title"] for r in res.json()["results"])


def test_mailbox_disabled(db, demo):
    from bis.connectors.mailbox import fetch_mailbox

    assert "skipped" in fetch_mailbox(db, demo, imap_factory=lambda cfg: None)


def test_mailbox_routes_production_attachments_to_ai(db, demo):
    from sqlalchemy import select

    from bis.connectors.mailbox import fetch_mailbox
    from bis.models import AIAction, Document, IntakeEvent

    demo.settings = {**demo.settings, "mailbox": {"enabled": True, "host": "imap.test", "username": "u"}}
    db.commit()
    upd = ("Универсальный передаточный документ\nСчет-фактура № 77 от 03.06.2026\nПродавец: ООО «БетонСтрой» "
           "ИНН 7705123456\n1 | Бетон B25 W8 F150 | м3 | 60 | 7 200,00 | 432 000,00").encode()
    msg = EmailMessage()
    msg["From"], msg["Subject"], msg["Message-ID"] = "ООО «БетонСтрой» <sales@beton.example>", "УПД за июнь", "<m9@test>"
    msg.set_content("Добрый день! Направляем УПД.")
    msg.add_attachment(upd, maintype="text", subtype="plain", filename="УПД_77.txt")
    imap = FakeImap({b"1": msg.as_bytes()})
    assert fetch_mailbox(db, demo, imap_factory=lambda cfg: imap) == {"imported": 1, "skipped": 0}
    ev = db.scalar(select(IntakeEvent).where(IntakeEvent.source == "email"))
    assert ev.detected_type == "upd" and ev.result["email"]["subject"] == "УПД за июнь"
    assert db.scalar(select(AIAction).where(AIAction.event_id == ev.id, AIAction.kind == "create_receipt")).status == "proposed"
    letter = db.scalar(select(Document).where(Document.kind == "incoming_letter", Document.source == "email"))
    assert letter.data["routed_to_ai"] == ["УПД_77.txt"]
    assert not db.scalar(select(Document).where(Document.title.like("Вложение:%")))  # не задвоено обычным вложением
