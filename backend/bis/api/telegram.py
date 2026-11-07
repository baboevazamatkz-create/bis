"""Telegram-бот: webhook, привязка аккаунта."""

import hmac
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from bis.api.deps import current_user
from bis.config import get_settings
from bis.connectors import telegram as tg
from bis.db import get_db
from bis.models import User

router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks,
                  x_telegram_bot_api_secret_token: str | None = Header(None)) -> dict[str, Any]:
    """Обновления от Telegram. Ответ — сразу, обработка — в фоне (модели могут работать дольше таймаута Telegram)."""
    s = get_settings()
    if not s.telegram_bot_token or not s.telegram_webhook_secret:
        raise HTTPException(403, "Webhook Telegram не настроен (BIS_TELEGRAM_BOT_TOKEN, BIS_TELEGRAM_WEBHOOK_SECRET)")
    if not hmac.compare_digest(x_telegram_bot_api_secret_token or "", s.telegram_webhook_secret):
        raise HTTPException(403, "Неверный секрет webhook")
    update = await request.json()
    background.add_task(tg.handle_update_safely, update)
    return {"ok": True}


@router.post("/link")
def link(user: User = Depends(current_user)) -> dict[str, Any]:
    """Код привязки: отправьте боту «/start <код>» (действует 15 минут)."""
    s = get_settings()
    if not s.telegram_bot_token:
        raise HTTPException(400, "Telegram-бот не подключён (BIS_TELEGRAM_BOT_TOKEN)")
    code = tg.link_code(user.id)
    return {"code": code, "command": f"/start {code}", "expires_in": tg.LINK_TTL, "linked": bool(user.telegram_chat_id),
            "deep_link": f"https://t.me/{s.telegram_bot_username}?start={code}" if s.telegram_bot_username else None}


@router.delete("/link")
def unlink(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict[str, bool]:
    u = db.get(User, user.id)
    u.telegram_chat_id = None
    db.commit()
    return {"linked": False}
