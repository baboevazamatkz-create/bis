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

## Демо-страница без сервера

Статическая демо-версия (интерфейс + снимок данных, записанный с работающего backend):

```bash
# 1. свежая демо-база и запущенный API
bis migrate && bis seed-demo && bis run-jobs && uvicorn bis.main:app --port 8000 &
# 2. снимок: действия (АОСР, КС-2/КС-3, М-29, схема, сверка ПД, чат) выполняются настоящим API
python backend/scripts/record_demo.py http://localhost:8000 demo-snapshot.json
# 3. сборка одной HTML-страницы
cd frontend && VITE_DEMO=1 npx vite build --outDir dist-demo && node scripts/pack-demo.mjs dist-demo ../demo-snapshot.json bis-demo.html
```

В демо-режиме запросы обслуживает `frontend/src/demo/mock.ts`; поиск по нормам работает в браузере,
документы показываются предпросмотром.

## Фоновые задачи

`bis worker` (сервис `worker` в Docker Compose) выполняет по расписанию:

| Задача | Период | Что делает |
|---|---|---|
| `deadlines` | 1 ч | Сроки обязательств и поручений: напоминания, просрочки, эскалация администратору после 7 дней |
| `plan_fact` | 1 сут | Отставания от ГПР и прогноз окончания — ответственному и руководителям |
| `mailbox` | 5 мин | Приём входящей почты по IMAP, разбор агентом «Секретарь» |

Однократный запуск: `bis run-jobs deadlines`; из интерфейса — «Контроль → Фоновые проверки».
Уведомления повторно не отправляются (ключ события). Telegram: `BIS_TELEGRAM_BOT_TOKEN` и `telegram_chat_id` у пользователя.

## Учёт работ и закрытие

1. Смета договора: импорт из Excel (`Работы и закрытие → Импорт сметы`) — заголовки и разделы определяются
   автоматически, скрытые работы помечаются.
2. ГПР, нормы расхода, приход и расход материалов — в разделе «Учёт работ и материалов».
3. Объёмы: прораб сообщает агенту «Контроль работ» («залили 250 м3 плиты Пм1, захватка 3») или вносит в реестр;
   ПТО подтверждает и привязывает АОСР к скрытым работам.
4. Закрытие: проверка за период → КС-2 и КС-3 (XLSX). Утверждение КС-2 закрывает объёмы; отклонение освобождает их.
5. Материалы: М-29 (расход по нормам с потерями против факта) и акт списания в пределах нормы с допуском 2 %.

## Голос

Сервер распознавания с OpenAI-совместимым API (профиль `voice`, faster-whisper) и `BIS_STT_URL`.
В чате появится кнопка микрофона; диктовка нормализуется («25 кубов» → «25 м3», «в осях 1 по 5» → «в осях 1-5»).
Озвучивание ответов — `BIS_TTS_URL` (совместимый `/audio/speech`).

## Оценка качества

```bash
bis eval --org-inn 7701000001                 # демо-набор: поиск (hit@k, MRR) и маршрутизация
bis eval my_cases.json --org-inn ... --answers  # плюс проверка ответов агентов (нужна модель)
```

Набор — JSON: `question`, `expect_citation`, `expect_agent`, `expect_contains`. Запускайте после загрузки
документов, смены модели или эмбеддингов. Термины компании для поиска добавляются в память организации
(тип «term»: `кротовина = обратная засыпка пазух`).

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
  documents/   формы документов: АОСР, письмо (DOCX), КС-2, КС-3, М-29, акт списания (XLSX)
  cad/         исполнительные схемы (DXF, SVG-предпросмотр)
  connectors/  внешние источники: почта IMAP
  jobs.py      фоновые задачи;  evals.py — оценка качества;  voice.py — голос
  plugins/     SDK плагинов
  services/    договоры, корреспонденция, работы и материалы, закрытие, ПД, схемы, уведомления
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
