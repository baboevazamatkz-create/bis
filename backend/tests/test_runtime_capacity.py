"""System regression: blocked cloud work must leave API/DB/settings capacity available."""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException


def test_repeated_chat_attempts_do_not_create_unbounded_threads(client, auth, demo, monkeypatch):
    from bis.api.chat import ChatIn, chat_stream
    from bis.db import SessionLocal, get_engine
    from bis.models import User
    from bis.runtime import chat_workers

    stop = threading.Event()
    entered = threading.Barrier(3)

    def blocked(*args, **kwargs):
        entered.wait(timeout=5)
        stop.wait(10)
        return {'ok': True}

    monkeypatch.setattr('bis.api.chat._answer', blocked)
    with SessionLocal() as db:
        from sqlalchemy import select
        user_id = db.scalar(select(User.id).where(User.organization_id == demo.id).limit(1))
    accepted = rejected = 0
    try:
        for _ in range(25):
            with SessionLocal() as db:
                user = db.get(User, user_id)
                try:
                    chat_stream(ChatIn(message='blocked'), db=db, user=user)
                    accepted += 1
                except HTTPException as error:
                    assert error.status_code == 503
                    rejected += 1
        entered.wait(timeout=5)
        assert accepted == 2 and rejected == 23
        assert get_engine().pool.checkedout() <= 2
        assert client.get('/api/auth/me', headers=auth).status_code == 200
        assert client.get('/api/workspace/reading-status', headers=auth).status_code == 200
    finally:
        stop.set()
        for thread in threading.enumerate():
            if thread.name == 'bis-chat':
                thread.join(5)
    assert chat_workers.snapshot()['active'] == 0


def test_initialization_timeout_does_not_spawn_more_clients_or_hold_settings(monkeypatch):
    from bis.config import get_settings
    from bis.llm.gateway import LLMGateway, get_gateway, set_gateway
    from bis.llm.types import LLMError
    from bis.runtime import model_initializers

    stop = threading.Event()
    entered = threading.Event()
    calls = []

    def blocked(cls, settings):
        calls.append(1)
        entered.set()
        stop.wait(5)
        return LLMGateway(entries={})

    monkeypatch.setattr(get_settings(), 'model_initialization_wait_seconds', .02)
    monkeypatch.setattr(LLMGateway, 'from_settings', classmethod(blocked))
    set_gateway(None)
    try:
        for _ in range(8):
            with pytest.raises(LLMError, match='Клиент модели ещё запускается'):
                get_gateway()
        assert entered.is_set() and len(calls) == 1
        assert model_initializers.snapshot()['active'] == 1
        replacement = LLMGateway(entries={})
        set_gateway(replacement)
        assert get_gateway() is replacement
    finally:
        stop.set()
        for thread in threading.enumerate():
            if thread.name == 'bis-model-init':
                thread.join(5)
    assert get_gateway() is replacement


def test_model_calls_are_bounded_and_offline_remains_available():
    from bis.llm.gateway import LLMGateway
    from bis.llm.providers.offline import OfflineProvider
    from bis.llm.types import LLMError, LLMResponse, Message
    from bis.runtime import model_requests

    stop = threading.Event()
    entered = threading.Barrier(5)

    class Blocked:
        model = 'fixture'
        def complete(self, *args, **kwargs):
            entered.wait(timeout=5)
            stop.wait(10)
            return LLMResponse(Message(role='assistant', content='OK'), 'end', 'fixture', 'fixture')

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(LLMGateway._call, Blocked(), [], None, 16, False, False) for _ in range(4)]
        try:
            entered.wait(timeout=5)
            for _ in range(20):
                with pytest.raises(LLMError, match='Модель занята'):
                    LLMGateway._call(Blocked(), [], None, 16, False, False)
            assert model_requests.snapshot()['active'] == 4
            assert LLMGateway._call(OfflineProvider('offline'), [], None, 16, False, False).provider == 'offline'
        finally:
            stop.set()
        for future in futures:
            future.result(timeout=5)
    assert model_requests.snapshot()['active'] == 0


def test_busy_write_capacity_keeps_health_reads_and_settings_accessible(client, auth, monkeypatch):
    from bis.runtime import http_operations

    monkeypatch.setattr('bis.api.workspace._save_ai_provider', lambda body: {'saved': True})
    for _ in range(http_operations.limit):
        assert http_operations.acquire()
    try:
        assert client.get('/api/health').json()['alive'] is True
        assert client.get('/api/auth/me', headers=auth).status_code == 200
        assert client.post('/api/ai/models/check', headers=auth).status_code == 503
        saved = client.post('/api/workspace/ai-provider', headers=auth,
                            json={'provider': 'openai', 'api_key': 'sk-proj-test'})
        assert saved.status_code == 200
    finally:
        for _ in range(http_operations.limit):
            http_operations.release()


def test_tcp_server_stays_responsive_with_abandoned_chat_workers(client, auth, demo, monkeypatch, tmp_path):
    """Run with BIS_SOAK_SECONDS=180 to verify survival over time, not only a single request."""
    import socket
    from contextlib import ExitStack

    import httpx
    import uvicorn

    from bis.config import get_settings
    from bis.db import get_engine
    from bis.main import create_app
    from bis.runtime import chat_workers

    stop = threading.Event()
    entered = threading.Barrier(3)
    def blocked(*args, **kwargs):
        entered.wait(timeout=10)
        stop.wait(float(os.environ.get('BIS_SOAK_SECONDS', '2')) + 30)
        return {'ok': True}

    monkeypatch.setattr('bis.api.chat._answer', blocked)
    keys = ('BIS_LOCAL_DIR', 'BIS_LLM_CONFIG_FILE', 'BIS_OPENAI_API_KEY', 'BIS_OCR_PROVIDER')
    previous = {key: os.environ.get(key) for key in keys}
    (tmp_path / '.env').write_text('UNRELATED=keep\n')
    os.environ['BIS_LOCAL_DIR'] = str(tmp_path)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(), log_level='error', timeout_graceful_shutdown=3))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True, name='bis-soak-server')
    thread.start()
    samples, latencies = 0, []
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', headers=auth, timeout=3) as http:
            with ExitStack() as streams:
                for _ in range(2):
                    response = streams.enter_context(http.stream('POST', '/api/chat/stream', json={'message': 'blocked'}))
                    assert response.status_code == 200
                    next(response.iter_lines())
                entered.wait(timeout=10)
            # Both browsers have disconnected; the actual model workers remain blocked.
            saved = http.post('/api/workspace/ai-provider', json={'provider': 'openai', 'api_key': 'sk-proj-soak-fixture'})
            assert saved.status_code == 200 and saved.json()['configuration']['selected_provider'] == 'openai'
            deadline = time.monotonic() + float(os.environ.get('BIS_SOAK_SECONDS', '2'))
            while time.monotonic() < deadline:
                for path in ('/api/auth/me', '/api/workspace/reading-status', '/api/objects', '/api/health'):
                    began = time.monotonic()
                    response = http.get(path)
                    latencies.append(time.monotonic() - began)
                    assert response.status_code == 200, response.text
                    samples += 1
                assert http.post('/api/chat/stream', json={'message': 'another'}).status_code == 503
                assert chat_workers.snapshot()['active'] == 2
                assert get_engine().pool.checkedout() <= 3  # two workers plus an HTTP dependency finishing cleanup
                time.sleep(.05)
            assert max(latencies) < 2
            stop.set()
            monkeypatch.setattr('bis.api.chat._answer', lambda *a, **kw: {'ok': True})
            until = time.monotonic() + 5
            while chat_workers.snapshot()['active'] and time.monotonic() < until:
                time.sleep(.01)
            assert chat_workers.snapshot()['active'] == 0
            recovered = http.post('/api/chat/stream', json={'message': 'recovered'})
            assert recovered.status_code == 200 and '"type": "done"' in recovered.text
            assert http.get('/api/workspace/reading-status').json()['selected_provider'] == 'openai'
            print(f'SOAK PASS: {samples} healthy HTTP responses, max latency {max(latencies):.3f}s; two stuck workers; no DB/thread growth.')
    finally:
        stop.set()
        server.should_exit = True
        thread.join(20)
        listener.close()
        for worker in threading.enumerate():
            if worker.name == 'bis-chat':
                worker.join(5)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


def test_stall_report_records_capacity_and_stacks_without_values(tmp_path, monkeypatch, caplog):
    from bis import middleware

    monkeypatch.setenv('BIS_LOCAL_DIR', str(tmp_path))
    monkeypatch.setattr(middleware, '_last_dump', -10000)
    secret_value = 'sk-proj-do-not-include-in-stack-report'
    middleware._record_stall('POST', '/api/ai/models/check')
    report = (tmp_path / 'logs/thread-dump.log').read_text()
    assert 'capacity=' in caplog.text and 'DB=' in caplog.text
    assert secret_value not in report and 'test_runtime_capacity' in report


def test_database_pool_exhaustion_returns_503_and_recovers(client, auth):
    from contextlib import ExitStack

    from bis.db import get_engine

    engine = get_engine()
    with ExitStack() as held:
        for _ in range(engine.pool.size() + engine.pool._max_overflow):
            held.enter_context(engine.connect())
        began = time.monotonic()
        assert client.get('/api/health').status_code == 200
        response = client.get('/api/auth/me', headers=auth)
        assert response.status_code == 503 and 'соединения с базой заняты' in response.json()['detail']
        assert time.monotonic() - began < 5
    assert client.get('/api/auth/me', headers=auth).status_code == 200


def test_background_models_leave_a_slot_for_interactive_work():
    from bis.llm.gateway import LLMGateway
    from bis.llm.providers.offline import ScriptedProvider
    from bis.llm.types import LLMResponse, Message
    from bis.runtime import background_work, model_requests

    stop = threading.Event()
    entered = threading.Barrier(4)
    class Blocked:
        model = 'fixture'
        def complete(self, *args, **kwargs):
            entered.wait(timeout=5)
            stop.wait(10)
            return LLMResponse(Message(role='assistant', content='done'), 'end', 'fixture', 'fixture')

    def background():
        with background_work():
            return LLMGateway._call(Blocked(), [], None, 16, False, False)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(background) for _ in range(3)]
        try:
            entered.wait(timeout=5)
            assert model_requests.snapshot()['active'] == 3
            response = LLMGateway._call(ScriptedProvider(['interactive']), [], None, 16, False, False)
            assert response.message.content == 'interactive'
        finally:
            stop.set()
        for future in futures:
            future.result(timeout=5)


def test_engine_cache_is_shared_by_none_and_default_calls():
    from bis.config import get_settings
    from bis.db import SessionLocal, get_engine

    assert get_engine() is get_engine(None) is get_engine(get_settings().database_url)
    with SessionLocal() as db:
        assert db.get_bind() is get_engine()
