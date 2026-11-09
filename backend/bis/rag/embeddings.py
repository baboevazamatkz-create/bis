"""Эмбеддинги.

- HashingEmbedder — без зависимостей и сети (лексическое сходство); для разработки и тестов.
- OpenAICompatEmbedder — сервер эмбеддингов (infinity, TEI, vLLM, Ollama) с bge-m3 и т.п.
- YandexEmbedder — смысловые эмбеддинги Yandex AI Studio (text-search-doc / text-search-query, 256 чисел).
  Модель работает в закрытом контуре — документы организации не покидают периметр.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Protocol

import httpx

from bis.config import Settings, get_settings

_WORD_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)


class Embedder(Protocol):
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def _stem(word: str) -> str:
    # грубый стемминг: обрезаем окончания, чтобы «засыпка/засыпки/засыпку» совпадали
    return word[:6] if len(word) > 6 else word


class HashingEmbedder:
    def __init__(self, dim: int = 1024):
        self.dim = dim

    def _features(self, text: str) -> dict[int, float]:
        text = text.lower().replace("ё", "е")
        feats: dict[int, float] = {}
        words = _WORD_RE.findall(text)
        for w in words:
            for token, weight in ((f"w:{_stem(w)}", 1.0), *((f"g:{w[i:i+4]}", 0.3) for i in range(max(len(w) - 3, 0)))):
                h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "little")
                idx = h % self.dim
                sign = 1.0 if (h >> 63) & 1 else -1.0
                feats[idx] = feats.get(idx, 0.0) + sign * weight
        return feats

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for idx, v in self._features(text).items():
            vec[idx] = v
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class OpenAICompatEmbedder:
    semantic = True

    def __init__(self, settings: Settings, client: httpx.Client | None = None, batch_size: int = 32):
        if not settings.embedding_url:
            raise ValueError("Не задан BIS_EMBEDDING_URL")
        self.dim = settings.embedding_dim
        self.model = settings.embedding_model
        self.query_prefix = settings.embedding_query_prefix
        self.doc_prefix = settings.embedding_document_prefix
        self.batch_size = batch_size
        headers = {}
        if settings.embedding_api_key:
            headers["Authorization"] = f"Bearer {settings.embedding_api_key}"
        self.client = client or httpx.Client(base_url=settings.embedding_url.rstrip("/"), headers=headers, timeout=120)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            r = self.client.post("/embeddings", json={"model": self.model, "input": batch})
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            for d in data:
                vec = d["embedding"]
                if len(vec) != self.dim:
                    raise ValueError(f"Размерность эмбеддинга {len(vec)} ≠ BIS_EMBEDDING_DIM={self.dim}")
                out.append(vec)
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self.doc_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self.query_prefix + text])[0]


class YandexEmbedder:
    """Смысловые эмбеддинги AI Studio: документы и запросы — разными моделями (асимметричный поиск)."""

    URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/textEmbedding"
    dim = 256
    semantic = True

    def __init__(self, settings: Settings, client: httpx.Client | None = None, workers: int = 4):
        folder, key = settings.yandex()
        if not folder:
            raise ValueError("Не задан каталог Yandex Cloud (BIS_YANDEX_FOLDER_ID / YANDEX_FOLDER_ID)")
        self.doc_model = f"emb://{folder}/text-search-doc/latest"
        self.query_model = f"emb://{folder}/text-search-query/latest"
        self.workers = workers
        self.client = client or httpx.Client(timeout=60, headers={"Authorization": f"Api-Key {key or 'proxy-injected'}"})

    def _one(self, model: str, text: str) -> list[float]:
        import time

        for attempt in range(5):
            r = self.client.post(self.URL, json={"modelUri": model, "text": text[:6000]})
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(1 + attempt * 2)
                continue
            r.raise_for_status()
            return [float(x) for x in r.json()["embedding"]]
        r.raise_for_status()
        raise RuntimeError("Yandex Embeddings: превышено число попыток")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(self.workers) as pool:
            return list(pool.map(lambda t: self._one(self.doc_model, t), texts))

    def embed_query(self, text: str) -> list[float]:
        return self._one(self.query_model, text)


_embedder: Embedder | None = None


def get_query_embedder() -> Embedder:
    """A broken embedding configuration must not prevent ordinary lexical searches."""
    try:
        return get_embedder()
    except (ValueError, TypeError):
        logging.getLogger(__name__).warning("Эмбеддинги не настроены: поиск продолжится по тексту")
        return HashingEmbedder(get_settings().embedding_dim)


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        s = get_settings()
        if s.embedding_provider == "yandex":
            _embedder = YandexEmbedder(s)
        elif s.embedding_provider == "openai_compat":
            _embedder = OpenAICompatEmbedder(s)
        else:
            _embedder = HashingEmbedder(s.embedding_dim)
    return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    global _embedder
    _embedder = embedder
