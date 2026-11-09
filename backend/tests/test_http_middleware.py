"""Disconnects must not become BaseHTTPMiddleware's 'No response returned' errors."""

import asyncio
import logging

import pytest
from starlette.middleware.base import BaseHTTPMiddleware

from bis.middleware import RequestLogMiddleware

SCOPE = {"type": "http", "method": "GET", "path": "/api/example", "headers": [], "query_string": b""}


async def disconnected():
    return {"type": "http.disconnect"}


def test_disconnect_before_headers_does_not_manufacture_server_error(caplog):
    async def app(scope, receive, send):
        assert (await receive())["type"] == "http.disconnect"

    sent = []

    async def send(message):
        sent.append(message)

    # Reproduce the exact failure of the former decorator-based middleware.
    async def dispatch(request, call_next):
        return await call_next(request)

    with pytest.raises(RuntimeError, match="No response returned"):
        asyncio.run(BaseHTTPMiddleware(app, dispatch=dispatch)(SCOPE.copy(), disconnected, send))
    sent.clear()
    asyncio.run(RequestLogMiddleware(app)(SCOPE.copy(), disconnected, send))
    assert not sent
    assert not caplog.records


def test_streaming_response_passes_through_before_request_finishes():
    sent = []

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        assert sent[-1]["body"] == b"first"
        await send({"type": "http.response.body", "body": b"last", "more_body": False})

    asyncio.run(RequestLogMiddleware(app)(SCOPE.copy(), disconnected, send))
    assert [m["body"] for m in sent if m["type"] == "http.response.body"] == [b"first", b"last"]


@pytest.mark.parametrize("started", [False, True])
def test_real_application_errors_still_propagate_and_are_logged(started, caplog):
    async def send(message):
        pass

    async def app(scope, receive, send):
        if started:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("No response returned.")

    with pytest.raises(RuntimeError, match="No response returned"):
        asyncio.run(RequestLogMiddleware(app)(SCOPE.copy(), disconnected, send))
    assert any(r.levelno == logging.ERROR and "/api/example" in r.message for r in caplog.records)


def test_error_status_is_logged_without_swallowing_the_response(caplog):
    sent = []

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 503, "headers": []})
        await send({"type": "http.response.body", "body": b"unavailable"})

    asyncio.run(RequestLogMiddleware(app)(SCOPE.copy(), disconnected, send))
    assert sent[-1]["body"] == b"unavailable"
    assert "503" in caplog.text


def test_cancellation_is_preserved_without_logging_a_server_error(caplog):
    async def send(message):
        pass

    async def app(scope, receive, send):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(RequestLogMiddleware(app)(SCOPE.copy(), disconnected, send))
    assert not caplog.records


def test_lifespan_passes_through():
    sent = []

    async def receive():
        return {"type": "lifespan.shutdown"}

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        assert (await receive())["type"] == "lifespan.shutdown"
        await send({"type": "lifespan.shutdown.complete"})

    asyncio.run(RequestLogMiddleware(app)({"type": "lifespan"}, receive, send))
    assert sent == [{"type": "lifespan.shutdown.complete"}]
