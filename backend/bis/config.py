"""Настройки платформы. Все параметры задаются переменными окружения с префиксом BIS_."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
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
    # Возможности модели: text, tools, vision (понимает изображения: фото, сканы, чертежи)
    capabilities: list[str] = Field(default_factory=lambda: ["text", "tools"])
    timeout: float = 120.0
    temperature: float | None = 0.2
    effort: str | None = None
    verify_ssl: bool | str = True
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("model", "base_url", mode="before")
    @classmethod
    def _expand_env(cls, v: Any) -> Any:
        """${YANDEX_FOLDER_ID} и подобные — из переменных окружения сервера (идентификаторы, не секреты)."""
        import os

        return os.path.expandvars(v) if isinstance(v, str) else v

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
    embedding_provider: Literal["hash", "openai_compat", "yandex"] = "hash"  # yandex — смысловые эмбеддинги AI Studio
    embedding_url: str | None = None
    embedding_model: str = "BAAI/bge-m3"
    embedding_api_key: str | None = None
    embedding_dim: int = 1024
    # pgvector: false — без векторного расширения (старые процессоры, встроенная БД): поиск полнотекстовый
    vector_enabled: bool = True
    embedding_query_prefix: str = ""
    embedding_document_prefix: str = ""

    # Реранкер (необязательно): Cohere-совместимый /rerank (infinity, vLLM, Jina)
    rerank_url: str | None = None
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # LLM: JSON-список ProviderConfig и маршруты задач -> упорядоченный список провайдеров
    llm_providers_json: str | None = None
    llm_config_file: Path | None = None
    llm_routes_json: str | None = None

    # Telegram-бот: приём фото, голосовых и документов с площадки, уведомления, решения по предложениям ИИ
    # сводка дня руководителям и напоминание прорабам (местное время BIS)
    digest_hour: int = 18
    remind_hour: int = 16
    digest_sunday: bool = False
    # вход без пароля с этого же компьютера (локальный запуск на время разработки)
    auto_login: bool = False
    telegram_bot_token: str | None = None
    telegram_bot_username: str | None = None  # для ссылки привязки t.me/<бот>?start=<код>
    telegram_webhook_secret: str | None = None  # заголовок X-Telegram-Bot-Api-Secret-Token
    telegram_api_base: str = "https://api.telegram.org"  # или локальный Bot API сервер в закрытом контуре
    public_url: str | None = None  # адрес веб-интерфейса — для ссылок в сообщениях

    # Yandex Cloud (SpeechKit, Vision OCR): каталог и переменная с ключом AI Studio
    yandex_folder_id: str | None = None  # BIS_YANDEX_FOLDER_ID или YANDEX_FOLDER_ID
    yandex_api_key_env: str = "YANDEX_API_KEY"
    ocr_provider: Literal["none", "yandex"] = "none"  # распознавание сканов и фото документов

    # Голос: stt_provider=openai — OpenAI-совместимый сервер в закрытом контуре; yandex — SpeechKit
    stt_provider: Literal["openai", "yandex"] = "openai"
    stt_url: str | None = None  # например http://stt:8000/v1 (faster-whisper / speaches)
    stt_model: str = "Systran/faster-whisper-large-v3"
    stt_language: str = "ru"
    stt_api_key: str | None = None
    tts_url: str | None = None
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"
    tts_api_key: str | None = None

    # Локальный запуск: папка собранного веб-интерфейса (API отдаёт его сам) и путь к ffmpeg
    web_dir: Path | None = None
    ffmpeg: str | None = None

    # Конвертация DXF → DWG (путь к ODA File Converter, необязательно)
    oda_converter: str | None = None

    # Плагины: None — все найденные
    enabled_plugins: list[str] | None = None

    def yandex(self) -> tuple[str | None, str | None]:
        """(каталог, ключ) для сервисов Yandex Cloud."""
        import os

        return (self.yandex_folder_id or os.environ.get("YANDEX_FOLDER_ID"),
                os.environ.get(self.yandex_api_key_env))

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
