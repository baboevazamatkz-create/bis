# Документация BIS

## Начало работы

- [Контекст продукта](PROJECT_CONTEXT.md) · [Архитектура](ARCHITECTURE.md) · [Текущее состояние](CURRENT_STATE.md)
- [Приоритеты и backlog](TASKS.md) · [Журнал решений](DECISIONS.md)
- [Запуск для разработчика](DEVELOPMENT.md) · [Изоляция PostgreSQL](TEST_DATABASE.md) · [Сквозная приёмка](ACCEPTANCE.md)
- [Правила агентов](../AGENTS.md) · [Роли AI-команды](AI_TEAM.md) · [Шаблон задания](templates/TASK.md) · [Шаблон передачи](templates/HANDOFF.md)

## Инструкции и подсистемы

| Область | Материалы |
|---|---|
| Локальный пилот | [Установка Windows](PILOT_WINDOWS.md) · [Рабочее место ПТО](PILOT_WORKSPACE.md) · [Диагностика ИИ](AI_RUNTIME_DIAGNOSTICS.md) · [Подключение OpenAI](OPENAI_SWITCH.md) |
| Linux и Docker | [Установка Yandex Cloud](INSTALL_YANDEX_CLOUD.md) описывает прежнюю ветку. Статус входа на рабочей ветке приведён в [текущем состоянии](CURRENT_STATE.md#открытые-ограничения). |
| Подготовка ИД | [Конвейер](EXECUTIVE_PIPELINE.md) · [Требования и комплектность](EXECUTIVE_REQUIREMENTS.md) · [Подготовка по образцам и источникам](ADAPTIVE_AUTHORING.md) |
| Данные и интеграции | [Библиотеки](LIBRARIES.md) · [Чтение проектов](PROJECT_UNDERSTANDING.md) · [Проектные факты](PROJECT_FACTS.md) · [CAD MCP](CAD_MCP.md) |

Точки входа и действующие маршруты сверяйте с [архитектурой](ARCHITECTURE.md) и кодом.
Предметные документы сохраняют описание подсистем, включая интерфейс на момент
их написания.

## Исторические планы, отчёты и проектные материалы

Эти документы сохраняют исходные критерии, результаты проверок и контекст
прежних решений. Актуальный статус уточняется в [текущем состоянии](CURRENT_STATE.md).

- Проектные материалы — [план проекта](PLAN.md), [развитие ядра](BRAIN_ROADMAP.md),
  [дизайн интерфейса](DESIGN.md), [описание модулей](MODULES.md).
- Ревизии — [аудит приложения](APPLICATION_AUDIT.md), [аудит интеллекта](INTELLIGENCE_AUDIT.md),
  [чтение источников](READING_INTEGRITY.md), [надёжность интерфейса](UI_RELIABILITY.md).
- Разборы исправлений — [тайм-ауты](RUNTIME_STABILITY.md),
  [устойчивость подключения ИИ](AI_CONNECTION_RELIABILITY.md),
  [переключение Anthropic](ANTHROPIC_SWITCH_AUDIT.md),
  [договоры и письма](CONTRACT_AND_LETTER_FIX.md).
