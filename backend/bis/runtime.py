"""Bound long-lived work so cloud failures cannot consume the whole API server."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar


class Capacity:
    def __init__(self, limit: int):
        self.limit = limit
        self._slots = threading.BoundedSemaphore(limit)
        self._lock = threading.Lock()
        self._active = 0

    def acquire(self) -> bool:
        if not self._slots.acquire(blocking=False):
            return False
        with self._lock:
            self._active += 1
        return True

    def release(self) -> None:
        with self._lock:
            self._active -= 1
        self._slots.release()

    def snapshot(self) -> dict:
        with self._lock:
            return {'active': self._active, 'limit': self.limit}


# Desktop pilot: keep DB connections and AnyIO worker threads for navigation/settings.
chat_workers = Capacity(2)
model_requests = Capacity(4)
background_model_requests = Capacity(3)
_background = ContextVar("bis_background", default=False)
model_initializers = Capacity(2)
http_operations = Capacity(6)


@contextmanager
def background_work():
    token = _background.set(True)
    try:
        yield
    finally:
        _background.reset(token)


def is_background() -> bool:
    return _background.get()


@contextmanager
def model_slot():
    from bis.llm.types import LLMError

    background_acquired = False
    if is_background():
        background_acquired = background_model_requests.acquire()
        if not background_acquired:
            raise LLMError('Фоновое чтение ожидает свободную модель.')
    acquired = model_requests.acquire()
    if not acquired:
        if background_acquired:
            background_model_requests.release()
        raise LLMError('Модель занята другими задачами. Дождитесь завершения текущих запросов; ключ сохранён.')
    try:
        yield
    finally:
        model_requests.release()
        if background_acquired:
            background_model_requests.release()


def snapshot() -> dict:
    return {name: capacity.snapshot() for name, capacity in (
        ('chat_workers', chat_workers), ('model_requests', model_requests),
        ('background_model_requests', background_model_requests),
        ('model_initializers', model_initializers), ('http_operations', http_operations))}
