# BIS — запуск и разработка

## Быстрый старт (Docker)

```bash
cp deploy/.env.example deploy/.env          # задайте BIS_SECRET_KEY и пароли
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml exec api bis seed-demo
```

Откройте http://localhost:8080. Демо-вход: `admin@demo.bis` / `demo12345`.

Без ключей моделей система работает в **офлайн-режиме**: поиск по нормам с цитатами, реестры,
формирование АОСР и писем по формам. Ответы в чате в этом режиме собираются из найденных фрагментов,
без генерации текста.

Профили:

- `--profile ai` — сервер эмбеддингов bge-m3. В `.env` укажите `BIS_EMBEDDING_PROVIDER=openai_compat`
  и `BIS_EMBEDDING_URL=http://embeddings:80/v1`. После смены эмбеддингов переиндексируйте базу знаний.
- `--profile gpu` — локальная LLM на vLLM (закрытый контур, конфиденциальные данные).

## Гибридная схема ИИ (РФ)

Модели описаны в `deploy/config/llm.json`. У каждого провайдера есть `max_sensitivity`:

| Уровень | Что это | Куда можно отправлять |
|---|---|---|
| `public` | нормативы, общие вопросы | любые провайдеры |
| `internal` | данные организации | облака РФ (GigaChat, YandexGPT) и локальная модель |
| `confidential` | ПДн, коммерческая тайна | только локальная модель |

- Если ПДн найдены автоматически, а у облачного провайдера включено `mask_pii`, ПДн заменяются метками
  до отправки и восстанавливаются в ответе.
- `routes` задаёт порядок провайдеров для каждой задачи; следующие в списке используются как резерв.
- GigaChat: ключ авторизации берётся из переменной `GIGACHAT_AUTH_KEY`. Для TLS укажите в `verify_ssl`
  путь к сертификату НУЦ Минцифры.
- YandexGPT: OpenAI-совместимый API AI Studio. В `model` укажите `gpt://<folder_id>/yandexgpt/latest`,
  ключ берётся из `YANDEX_API_KEY`. Если ключ не принимается со схемой `Bearer`, задайте `"auth_scheme": "Api-Key"`.
- Claude (`"kind": "anthropic"`, модель `claude-opus-5-5`) подключается там, где сервис доступен легально.
  Серверный fallback при отказе модели включён.

## Локальная разработка

Нужны Python 3.11+, Node 20+ и PostgreSQL 16 с pgvector.

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export BIS_DATABASE_URL=postgresql+psycopg://bis:bis@localhost:5432/bis
bis migrate && bis seed-demo
uvicorn bis.main:app --reload          # API: http://localhost:8000/docs

cd ../frontend && npm install && npm run dev   # http://localhost:5173
```

Тесты (нужна БД `bis_test`): `TEST_DATABASE_URL=postgresql+psycopg://bis:bis@localhost:5432/bis_test pytest`.

## Загрузка нормативной базы

```bash
bis ingest ./norms --kind norm                      # общая библиотека платформы
bis ingest ./company_docs --kind org_document --org-inn 7701000001
```

Обозначение (СП, ГОСТ, СНиП) определяется автоматически. Нормативы разбиваются по пунктам, поэтому
ответы ссылаются на конкретный пункт: «СП 70.13330.2012, п. 5.16.3». Отменённые документы помечаются
в интерфейсе; агенты предупреждают, если ссылаются на них.

## Структура

```
backend/bis/
  api/         REST API (FastAPI)
  agents/      фреймворк агентов, супервизор, встроенные агенты и инструменты
  llm/         LLM-шлюз, маршрутизация, маскирование ПДн, провайдеры
  rag/         парсеры, разбиение на фрагменты, эмбеддинги, гибридный поиск
  documents/   формы документов (АОСР, письмо), шаблоны организаций
  plugins/     SDK плагинов
  services/    хранилище, аудит, документооборот
  models/      модели БД;  migrations/ — Alembic
frontend/      веб-интерфейс (React + TypeScript)
deploy/        docker-compose, конфигурация моделей
```

## Плагины

Плагин — Python-пакет с точкой входа `bis.plugins`:

```python
from pydantic import BaseModel
from bis.agents import Agent, tool
from bis.plugins import Plugin, PluginManifest

class EstimateArgs(BaseModel):
    work: str

@tool("estimate_cost", "Оценка стоимости работ", scopes=("registry:read",))
def estimate_cost(ctx, args: EstimateArgs) -> dict:
    return {"work": args.work, "cost": None}

class EstimatesPlugin(Plugin):
    manifest = PluginManifest(name="estimates", version="0.1.0", title="Сметы", permissions=["registry:read"])

    def tools(self):
        return [estimate_cost]

    def agents(self):
        return [Agent(name="estimator", title="Сметчик", description="Сметы и КС-2",
                      instructions="…", tools=["estimate_cost", "search_knowledge"])]
```

```toml
[project.entry-points."bis.plugins"]
estimates = "bis_estimates:EstimatesPlugin"
```

Инструменту плагина доступны только права (scopes), объявленные в манифесте. Перед вызовом инструмента
права проверяются по роли пользователя. Каталог инструментов в формате MCP: `GET /api/tools`.
