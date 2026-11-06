"""Файловое хранилище. Локальная ФС сейчас; S3/MinIO — та же абстракция (фаза 2)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from bis.config import get_settings

_SAFE = re.compile(r"[^\w.\-]+", re.UNICODE)


class Storage(Protocol):
    def put(self, data: bytes, filename: str, *, prefix: str) -> str: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Недопустимый ключ файла")
        return path

    def put(self, data: bytes, filename: str, *, prefix: str) -> str:
        now = datetime.now(UTC)
        safe = _SAFE.sub("_", filename)[-150:] or "file"
        key = f"{prefix}/{now:%Y/%m}/{uuid.uuid4().hex}_{safe}"
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = LocalStorage(get_settings().storage_dir)
    return _storage


def set_storage(storage: Storage | None) -> None:
    global _storage
    _storage = storage
