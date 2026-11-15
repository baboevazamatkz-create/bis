"""Request logging without buffering responses or creating a second receive stream."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("bis.http")
_dump_lock = threading.Lock()
_last_dump = 0.0


def _record_stall(method, path):
    global _last_dump
    from bis.db import get_engine
    from bis.runtime import snapshot

    log.error('Запрос всё ещё выполняется: %s %s; capacity=%s; DB=%s',
              method, path, snapshot(), get_engine().pool.status())
    home = os.environ.get('BIS_LOCAL_DIR')
    if not home:
        return
    with _dump_lock:
        if time.monotonic() - _last_dump < 60:
            return
        _last_dump = time.monotonic()
        try:
            import faulthandler
            from pathlib import Path

            path = Path(home) / 'logs' / 'thread-dump.log'
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 2_000_000:
                path.replace(path.with_suffix('.previous.log'))
            with path.open('a') as output:
                output.write('\nSlow request; inspect bis.log for route and capacity.\n')
                output.flush()
                faulthandler.dump_traceback(file=output, all_threads=True)
        except Exception:  # noqa: BLE001 — diagnosis must not take down the server
            log.exception('Не удалось записать стеки потоков')



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

        async def watch():
            await asyncio.sleep(20)
            _record_stall(scope['method'], scope['path'])

        watchdog = asyncio.create_task(watch())
        try:
            await self.app(scope, receive, send_logged)
        except Exception:
            log.exception("Ошибка запроса %s %s", scope["method"], scope["path"])
            raise
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)

        took = time.perf_counter() - started
        if status is not None and status >= 500:
            log.error("%s %s → %s за %.1f с", scope["method"], scope["path"], status, took)
        elif status is not None and took > 8:
            log.warning("медленно: %s %s — %.1f с", scope["method"], scope["path"], took)


class AdmissionMiddleware:
    """Reject excess long operations before they occupy authentication DB sessions/AnyIO threads."""
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        from starlette.responses import JSONResponse

        from bis.runtime import http_operations

        fast = {'/api/workspace/ai-provider', '/api/workspace/ai-setup', '/api/auth/login'}
        limited = (scope['type'] == 'http' and scope.get('method') in ('POST', 'PUT', 'PATCH', 'DELETE')
                   and scope.get('path', '').startswith('/api/') and scope['path'] not in fast)
        if not limited:
            return await self.app(scope, receive, send)
        if not http_operations.acquire():
            response = JSONResponse(status_code=503, content={'detail':
                'BIS занят длительными операциями. Повторите после их завершения; настройки подключения сохранены.'},
                headers={'Retry-After': '2'})
            return await response(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        finally:
            http_operations.release()
