"""Настройки платформы. Все параметры задаются переменными окружения с префиксом BIS_."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Sensitivity = Literal["public", "internal", "confidential"]


class ProviderConfig(BaseModel):
    """Описание LLM-провайдера.

    kind:
      - openai_compat — vLLM, Ollama, YandexGPT (AI Studio), любые OpenAI-совместимые API;
      - gigachat      — GigaChat (Сбер);
      - anthropic     — Claude (там, где доступен легально);
      - offline       — без модели: извлекающие ответы по найденным фрагментам (демо/резерв);
      - scripted      — для тестов.
    max_sensitivity — максимальный уровень данных, которые можно отправлять провайдеру:
      public (нормативы) < internal (данные организации) < confidential (ПДн, коммерческая тайна).
    """

    name: str
    kind: Literal["openai_compat", "gigachat", "anthropic", "offline", "scripted"]
    model: str = ""
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    auth_scheme: str = "Bearer"
    max_sensitivity: Sensitivity = "internal"
    mask_pii: bool = False
    timeout: float = 120.0
    temperature: float | None = 0.2
    effort: str | None = None
    verify_ssl: bool | str = True
    extra: dict[str, Any] = Field(default_factory=dict)

    def resolved_api_key(self) -> str | None:
        import os

        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env)
        return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIS_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql+psycopg://bis:bis@localhost:5432/bis"
    secret_key: str = "change-me-in-production"  # в prod — не короче 32 символов
    access_token_ttl_minutes: int = 60 * 12
    storage_dir: Path = Path("./data/files")
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]

    # Эмбеддинги
    embedding_provider: Literal["hash", "openai_compat"] = "hash"
    embedding_url: str | None = None
    embedding_model: str = "BAAI/bge-m3"
    embedding_api_key: str | None = None
    embedding_dim: int = 1024
    embedding_query_prefix: str = ""
    embedding_document_prefix: str = ""

    # Реранкер (необязательно): Cohere-совместимый /rerank (infinity, vLLM, Jina)
    rerank_url: str | None = None
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # LLM: JSON-список ProviderConfig и маршруты задач -> упорядоченный список провайдеров
    llm_providers_json: str | None = None
    llm_config_file: Path | None = None
    llm_routes_json: str | None = None

    # Уведомления в Telegram (бот организации); chat_id задаётся у пользователя
    telegram_bot_token: str | None = None

    # Голос (OpenAI-совместимые серверы в закрытом контуре)
    stt_url: str | None = None  # например http://stt:8000/v1 (faster-whisper / speaches)
    stt_model: str = "Systran/faster-whisper-large-v3"
    stt_language: str = "ru"
    stt_api_key: str | None = None
    tts_url: str | None = None
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"
    tts_api_key: str | None = None

    # Конвертация DXF → DWG (путь к ODA File Converter, необязательно)
    oda_converter: str | None = None

    # Плагины: None — все найденные
    enabled_plugins: list[str] | None = None

    def llm_providers(self) -> list[ProviderConfig]:
        raw: list[dict[str, Any]] | None = None
        if self.llm_config_file and self.llm_config_file.exists():
            data = json.loads(self.llm_config_file.read_text(encoding="utf-8"))
            raw = data.get("providers")
        elif self.llm_providers_json:
            raw = json.loads(self.llm_providers_json)
        if not raw:
            # Без настроек работаем в офлайн-режиме: поиск и генерация форм доступны, ответы — извлекающие.
            return [ProviderConfig(name="offline", kind="offline", max_sensitivity="confidential")]
        return [ProviderConfig.model_validate(p) for p in raw]

    def llm_routes(self) -> dict[str, list[str]]:
        if self.llm_config_file and self.llm_config_file.exists():
            data = json.loads(self.llm_config_file.read_text(encoding="utf-8"))
            if data.get("routes"):
                return data["routes"]
        if self.llm_routes_json:
            return json.loads(self.llm_routes_json)
        return {}


@lru_cache
def get_settings() -> Settings:
    return Settings()
