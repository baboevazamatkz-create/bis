"""Приём входящей почты по IMAP: письмо → реестр → база знаний → разбор агентом «Секретарь».

Производственные вложения (УПД, сертификаты, протоколы испытаний, съёмка, проект, договор, фото) уходят
в ИИ-конвейер: приход материалов, привязка сертификатов, замечания и т.д. — как если бы их прислали с объекта.

Настройка в организации (settings.mailbox):
    {"enabled": true, "host": "imap.example.ru", "port": 993, "username": "office@example.ru",
     "password_env": "BIS_MAILBOX_PASSWORD", "folder": "INBOX"}
Пароль хранится в переменной окружения сервера, не в БД.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import email
import email.policy
import imaplib
import logging
import os
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bis.ai import conveyor as cv
from bis.ai import understanding as und
from bis.jobs import job
from bis.llm.gateway import get_gateway
from bis.models import Document, Organization
from bis.rag.embeddings import get_embedder
from bis.rag.ingest import ingest_bytes
from bis.rag.parsers import UnsupportedFormat, parse_file
from bis.services.correspondence import process_incoming
from bis.services.documents import save_document
from bis.services.storage import get_storage

log = logging.getLogger(__name__)
MAX_MESSAGES = 50
PRODUCTION_TYPES = {"upd", "certificate", "test_protocol", "survey", "project_doc", "contract"}
ImapFactory = Callable[[dict[str, Any]], Any]


def default_imap(cfg: dict[str, Any]):
    client = imaplib.IMAP4_SSL(cfg["host"], int(cfg.get("port", 993)))
    password = os.environ.get(cfg.get("password_env", ""), "") or cfg.get("password", "")
    client.login(cfg["username"], password)
    return client


def import_message(db: Session, org: Organization, raw: bytes, *, gateway=None) -> Document | None:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    message_id = str(msg.get("Message-ID", "")).strip()
    if message_id and db.scalar(select(Document.id).where(Document.organization_id == org.id,
                                                          Document.data["message_id"].astext == message_id)):
        return None  # уже загружено
    subject = str(msg.get("Subject", "")) or "Письмо без темы"
    storage, embedder = get_storage(), get_embedder()
    doc = save_document(db, storage, org_id=org.id, kind="incoming_letter", title=subject, data={"message_id": message_id,
                        "from": str(msg.get("From", ""))}, file_bytes=raw, file_name="letter.eml", created_by="system:mailbox",
                        mime="message/rfc822")
    doc.source = "email"
    ingest_bytes(db, storage, embedder, org_id=org.id, data=raw, filename="letter.eml", kind="letter", title=subject,
                 document_id=doc.id, mime="message/rfc822")
    texts, routed = [], []
    for part in msg.iter_attachments():
        name = part.get_filename()
        payload = part.get_payload(decode=True)
        if not name or not payload:
            continue
        if to_conveyor(db, org, gateway, name=name, data=payload, mime=part.get_content_type(), subject=subject,
                       sender=str(msg.get("From", "")), message_id=message_id):
            routed.append(name)
            continue
        att = save_document(db, storage, org_id=org.id, kind="other", title=f"Вложение: {name}",
                            data={"parent_id": str(doc.id)}, file_bytes=payload, file_name=name, created_by="system:mailbox",
                            mime=part.get_content_type())
        att.source, att.status = "email", "registered"
        try:
            src = ingest_bytes(db, storage, embedder, org_id=org.id, data=payload, filename=name, kind="letter",
                               title=f"{subject} — {name}", document_id=att.id, mime=part.get_content_type())
            if src.chunk_count:
                from bis.models import KnowledgeChunk

                texts += [c.content for c in db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.source_id == src.id)
                                                        .order_by(KnowledgeChunk.ord).limit(20))]
        except UnsupportedFormat:
            continue
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body is not None else ""
    if texts:
        text += "\n\nТекст вложений:\n" + "\n".join(texts)[:20000]
    if routed:
        doc.data = {**(doc.data or {}), "routed_to_ai": routed}
    process_incoming(db, storage, gateway, doc, actor="agent:secretary", text=text,
                     sender_hint=str(msg.get("From", "")), subject_hint=subject)
    return doc


def to_conveyor(db: Session, org: Organization, gateway, *, name: str, data: bytes, mime: str | None, subject: str,
                sender: str, message_id: str) -> bool:
    """Производственный документ или фото из письма → ИИ-конвейер. False — обычное вложение письма."""
    kind = cv.detect_kind(name, mime)
    if kind == "document":
        try:
            text = parse_file(data, name, mime).text
        except UnsupportedFormat:
            return False
        if und.classify(gateway, text, name, "document")[0] not in PRODUCTION_TYPES:
            return False
    elif kind != "photo":
        return False
    env = cv.Env(db=db, storage=get_storage(), gateway=gateway, embedder=get_embedder(), org_id=org.id)
    event = cv.ingest(env, source="email", data=data, file_name=name, mime=mime, sender=sender[:300],
                      text=f"Вложение письма «{subject}»")
    event.result = {"email": {"message_id": message_id, "subject": subject[:300]}}
    cv.process(env, event)
    return True


def fetch_mailbox(db: Session, org: Organization, imap_factory: ImapFactory = default_imap) -> dict[str, Any]:
    cfg = (org.settings or {}).get("mailbox") or {}
    if not cfg.get("enabled"):
        return {"skipped": "почта не настроена"}
    client = imap_factory(cfg)
    imported = skipped = 0
    try:
        client.select(cfg.get("folder", "INBOX"))
        _, data = client.search(None, "UNSEEN")
        ids = (data[0] or b"").split()[:MAX_MESSAGES]
        for msg_id in ids:
            _, parts = client.fetch(msg_id, "(RFC822)")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw is None:
                continue
            doc = import_message(db, org, raw, gateway=get_gateway())
            db.commit()
            client.store(msg_id, "+FLAGS", "\\Seen")
            imported, skipped = imported + (doc is not None), skipped + (doc is None)
    finally:
        with contextlib.suppress(Exception):
            client.logout()
    return {"imported": imported, "skipped": skipped}


@job("mailbox", "Приём входящей почты (IMAP)", every_minutes=5)
def mailbox_job(db: Session, org: Organization, today: dt.date) -> dict[str, Any]:
    return fetch_mailbox(db, org)
