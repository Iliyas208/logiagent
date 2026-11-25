# LogiAgent

ИИ-агенты для обработки заявок, накладных и обращений клиентов в логистике.

## Описание

Веб-система для небольших экспедиторских компаний. Агенты на базе LLM берут на себя рутину сотрудников, а сложные случаи передают человеку:

1. **Агент заявок** достаёт маршрут, вес, дату, тип машины и адрес из текста клиента, считает уверенность и подбирает перевозчика с ценой. Если уверенность ниже порога (80%), заявка уходит менеджеру.
2. **Агент поддержки** отвечает клиенту о статусе груза. Он сам решает, какие инструменты вызвать: статус заявки, список заявок, база знаний, передача менеджеру.
3. **Агент документов** сверяет накладную с заявкой (в Milestone 2 показан на демо-данных, полная версия в Milestone 3).

Проект является продолжением дипломной темы «Разработка информационной системы автоматизации бизнес-процессов предприятия с использованием ИИ-агентов».

## Команда

| Участник | Роль |
|---|---|
| Rakhymzhan Iliyas | Team Lead, AI-агенты |
| Kakharman Dias | Backend, DevOps / QA |
| Keneskhanov Yerkebulan | ML, Frontend |

Контакт тимлида: Telegram @Rakhymzhaniliyas

## Запуск

Нужен Python 3.11+.

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # затем впишите ключи, см. ниже
.venv/bin/uvicorn app.main:app --reload
```

Откройте http://localhost:8000 и войдите под одной из демо-ролей (пароль у всех `demo123`):

| Роль | Почта | Что доступно |
|---|---|---|
| Клиент | client@logiagent.test | новая заявка, мои заявки, чат с агентом поддержки |
| Менеджер | manager@logiagent.test | очередь заявок, где агент не уверен |
| Владелец | owner@logiagent.test | тариф, лимит заявок, оплата |
| Администратор | admin@logiagent.test | всё, плюс журнал решений и метрики |

Через Docker: `docker compose up --build`.

### Ключи (backend/.env)

- `GROQ_API_KEY` — **бесплатный** вариант: ключ Groq (https://console.groq.com/keys). Агенты работают на модели `GROQ_MODEL` (по умолчанию `openai/gpt-oss-120b`).
- `ANTHROPIC_API_KEY` — ключ Claude API (платный, https://console.anthropic.com). Если задан, используется вместо Groq. Модель — `CLAUDE_MODEL` (по умолчанию `claude-opus-5-5`; дешевле — `claude-haiku-5-5`).
- Без обоих ключей агенты работают на запасных правилах, и интерфейс прямо показывает «Агент: правила». Выбрать провайдера явно: `AGENT_MODE=groq`, `claude` или `rules`.
- `STRIPE_SECRET_KEY` — **только тестовый** ключ Stripe (`sk_test_...`), живые ключи сервер не принимает. Без ключа работает встроенная демо-оплата. С ключом тариф оплачивается через Stripe Checkout тестовой картой `4242 4242 4242 4242`.
- `STRIPE_WEBHOOK_SECRET` — необязательно. Для вебхука `checkout.session.completed` на `/api/stripe/webhook` (локально: `stripe listen --forward-to localhost:8000/api/stripe/webhook`). Без него тариф подтверждается при возврате со страницы Stripe.

Файл `.env` в git не попадает.

### Тесты

```bash
cd backend
.venv/bin/python -m pytest -q
```

LLM и Stripe в тестах подменены, ключи и интернет не нужны. Тесты запускаются в GitHub Actions на каждый push.

## Стек

- Backend: Python, FastAPI, SQLAlchemy
- БД: SQLite для локального запуска, PostgreSQL через `DATABASE_URL` (pgvector для RAG — Milestone 3)
- LLM: Claude API (структурированный вывод для агента заявок, вызов инструментов для агента поддержки)
- Оплата: Stripe Checkout, тестовый режим, подписка по тарифам
- Frontend: одна страница на HTML/JS по макету из Figma (переход на React + TypeScript — Milestone 3)
- DevOps: Docker, GitHub Actions

## Структура

```
frontend/index.html     веб-кабинет: главная, вход, клиент, менеджер, тарифы, оплата, журнал
backend/app/main.py     API: вход и роли, заявки, очередь, поддержка, оплата, журнал
backend/app/agents/     intake.py (агент заявок), support.py (агент поддержки), rules.py (запасной режим), geo.py (расстояния, перевозчики)
backend/app/billing.py  Stripe Checkout и демо-оплата
backend/app/db.py       модели БД и демо-данные
backend/tests/          тесты API
docs/                   документация, задачи, схемы
ml/                     модели и эксперименты
```

## Ветки

- `main` — стабильная версия
- `dev` — основная ветка разработки; фичи вливаются через pull request
