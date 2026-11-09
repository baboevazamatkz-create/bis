"""Request logging without buffering responses or creating a second receive stream."""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("bis.http")


class RequestLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status: int | None = None

        async def send_logged(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_logged)
        except Exception:
            log.exception("Ошибка запроса %s %s", scope["method"], scope["path"])
            raise

        took = time.perf_counter() - started
        if status is not None and status >= 500:
            log.error("%s %s → %s за %.1f с", scope["method"], scope["path"], status, took)
        elif status is not None and took > 8:
            log.warning("медленно: %s %s — %.1f с", scope["method"], scope["path"], took)
