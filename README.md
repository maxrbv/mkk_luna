# Payments Service

[![CI](https://github.com/maxrbv/mkk_luna/actions/workflows/ci.yml/badge.svg)](https://github.com/maxrbv/mkk_luna/actions/workflows/ci.yml)

Асинхронный сервис процессинга платежей. Принимает запросы на оплату, обрабатывает их через эмуляцию платёжного шлюза и уведомляет клиента через webhook.

## Стек

FastAPI · Pydantic v2 · SQLAlchemy 2 (async) · PostgreSQL · RabbitMQ (FastStream) · Alembic · Docker Compose · Python 3.13.

## Архитектура

```
 API (FastAPI)                 Relay (отд. сервис)            Consumer (FastStream)
  │                             │                              │
  ├─ POST /payments             ├─ пуллит outbox               ├─ payments.new (quorum)
  │   INSERT payment            │   (FOR UPDATE SKIP LOCKED)   │   sleep 2-5s, 90/10
  │   INSERT outbox (same tx)   │   publish → RabbitMQ         │   UPDATE payment.status
  │                             │   DLQ depth monitor          │   POST webhook (retry x3)
  ├─ GET /payments/{id}         │                              │
  ├─ /health /readiness         └─ max_attempts → FAILED       └─ at-least-once → DLQ
  └─ rate limit POST
```

Миграции применяются отдельным init-контейнером `migrator` перед стартом API/relay/consumer.

## Запуск

```bash
cp config.example.yaml config.yaml
docker compose up --build
```

- API: `http://localhost:8000`
- RabbitMQ management UI: `http://localhost:15672` (guest/guest)
- Postgres: `localhost:5433`

## Сервисы в compose

- `postgres` + `rabbitmq` — с healthcheck'ами
- `migrator` — одноразовый `alembic upgrade head`, остальные ждут `service_completed_successfully`
- `api` — FastAPI/uvicorn
- `relay` — outbox-relay + DLQ-монитор
- `consumer` — FastStream worker, у consumer/relay heartbeat-файлы для docker healthcheck

## Примеры

### Создание платежа

```bash
curl -X POST http://localhost:8000/api/v1/payments \
  -H "X-API-Key: change-me-static-api-key" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{
    "amount": "199.99",
    "currency": "USD",
    "description": "Test payment",
    "metadata": {"order_id": "42"},
    "webhook_url": "https://webhook.site/<your-id>"
  }'
```

Ответ `202 Accepted`:
```json
{"id": "a90ff827-…", "status": "pending", "created_at": "2026-04-19T12:00:00Z"}
```

### Получение платежа

```bash
curl http://localhost:8000/api/v1/payments/a90ff827-… \
  -H "X-API-Key: change-me-static-api-key"
```

Ответ `200 OK`:
```json
{
  "id": "a90ff827-…",
  "amount": "199.99",
  "currency": "USD",
  "description": "Test payment",
  "metadata": {"order_id": "42"},
  "status": "succeeded",
  "idempotency_key": "e8f9...",
  "webhook_url": "https://webhook.site/<your-id>",
  "created_at": "2026-04-19T12:00:00Z",
  "processed_at": "2026-04-19T12:00:03.512Z"
}
```

### Идемпотентный повтор

Тот же `Idempotency-Key` → тот же `id`, новое тело **игнорируется**:

```bash
# Первый вызов — создаёт
curl -X POST http://localhost:8000/api/v1/payments \
  -H "X-API-Key: change-me-static-api-key" \
  -H "Idempotency-Key: order-42" \
  -H "Content-Type: application/json" \
  -d '{"amount":"10.00","currency":"USD","webhook_url":"https://h.example/hook"}'
# → 202  {"id":"a90ff827-…", "status":"pending", ...}

# Повтор с тем же ключом и другим body — вернёт оригинальный платёж
curl -X POST http://localhost:8000/api/v1/payments \
  -H "X-API-Key: change-me-static-api-key" \
  -H "Idempotency-Key: order-42" \
  -H "Content-Type: application/json" \
  -d '{"amount":"999.99","currency":"EUR","webhook_url":"https://attacker.example/"}'
# → 202  {"id":"a90ff827-…",  // тот же id
#        "status":"succeeded", // если уже обработан — настоящий статус
#        "created_at":"…"}     // оригинальный created_at
```

### Webhook payload

Сервис отправляет `POST` на `webhook_url` из платежа:

```json
{"payment_id": "a90ff827-…", "status": "succeeded"}
```

- `Content-Type: application/json`
- 2xx — ack, больше не ретраим
- 4xx — terminal, retry нет смысла (клиент явно отклонил)
- 5xx / network error / timeout — до 3 попыток с экспоненциальной задержкой, затем исключение

### Ошибки

```bash
# 401 — нет/неверный API-ключ
curl -i -X POST http://localhost:8000/api/v1/payments \
  -H "Content-Type: application/json" -d '{...}'
# HTTP/1.1 401 Unauthorized
# {"detail":"Invalid or missing X-API-Key"}

# 400 — забыт Idempotency-Key
curl -i -X POST http://localhost:8000/api/v1/payments \
  -H "X-API-Key: change-me-static-api-key" \
  -H "Content-Type: application/json" \
  -d '{"amount":"10","currency":"RUB","webhook_url":"https://h.example/"}'
# HTTP/1.1 400 Bad Request
# {"detail":"Idempotency-Key header is required"}

# 422 — валидация (amount сверх лимита, неизвестная валюта, невалидный URL)
curl -i -X POST http://localhost:8000/api/v1/payments \
  -H "X-API-Key: change-me-static-api-key" \
  -H "Idempotency-Key: validation-1" \
  -H "Content-Type: application/json" \
  -d '{"amount":"9999999999.99","currency":"USD","webhook_url":"https://h.example/"}'
# HTTP/1.1 422 Unprocessable Entity
# {"detail":[{"type":"less_than_equal", "loc":["body","amount"], ...}]}

# 429 — rate limit исчерпан (burst > rate_limit_capacity)
# HTTP/1.1 429 Too Many Requests
# {"detail":"Too many requests"}

# 404 — неизвестный payment_id
curl -i http://localhost:8000/api/v1/payments/00000000-0000-0000-0000-000000000000 \
  -H "X-API-Key: change-me-static-api-key"
# HTTP/1.1 404 Not Found
```

### Health / readiness

```bash
curl http://localhost:8000/health       # liveness (always 200)
curl http://localhost:8000/readiness    # 200, либо 503 если БД недоступна
```

### Наблюдение для отладки

```bash
# Состояние outbox (через Compose)
docker compose exec postgres psql -U payments -d payments \
  -c "SELECT id, status, attempts, last_error, published_at FROM outbox ORDER BY created_at DESC LIMIT 10;"

# Глубина DLQ (+ всё про очереди и обменники)
open http://localhost:15672    # guest / guest

# Структурированные логи с корреляцией по payment_id
docker compose logs consumer | grep '"payment_id": "a90ff827-…"'
```

## Решения требований

### Идемпотентность без race condition

`POST /payments` использует `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING`. Если RETURNING пусто — `SELECT` существующий. Это безопасно при конкурентных запросах с одним ключом (unique constraint + атомарный INSERT). В той же транзакции пишется `outbox`-событие — API никогда не публикует в брокер напрямую.

### Outbox с ограничением попыток

Relay пуллит `outbox.status = 'pending'` через `FOR UPDATE SKIP LOCKED` — это безопасно для горизонтального масштабирования relay-сервисов (нет двойных публикаций). После `max_publish_attempts` неудач событие переводится в `FAILED` и relay его больше не трогает. Partial-индекс `WHERE status = 'pending'` — единственный shape запроса.

### Quorum queue + at-least-once DLX

Основная `payments.new` — **quorum queue** с `x-dead-letter-strategy=at-least-once`, `x-overflow=reject-publish`, `x-delivery-limit=3`. DLQ — тоже quorum. Это жёсткое требование для at-least-once: без него quorum queue с дефолтной `at-most-once` **теряет** сообщения при `reject(requeue=false)`. Этот дефект был найден RMQ-тестом и починен в топологии, а не в коде.

### Consumer идемпотентный на re-delivery

Если на повторной доставке payment уже `succeeded`/`failed` — consumer **не перегенерирует** результат (иначе при flakey webhook вместо доставки уведомления мы бы переопределили гейтвейный исход). Повторно отправляется только webhook с уже зафиксированным статусом.

### Relay — отдельный сервис

Не background task в API lifespan: падение API не останавливает доставку событий. Плюс relay живёт сам, и в нём же запущен DLQ-depth monitor.

### Webhook-retry с уважением семантики HTTP

`tenacity` ретраит только на 5xx / network-ошибку. 4xx → terminal, retry не делаем (клиент явно отклонил) — тратить попытки на 401/400/422 бессмысленно.

### Static API-key через `secrets.compare_digest`

Constant-time сравнение — защита от timing-attack. Мелочь, но для auth-кода обязательная.

### Heartbeat-файлы вместо HTTP health

Relay и consumer — не HTTP-сервисы, тащить aiohttp ради `/health` не хотелось. Процесс каждые 5 сек обновляет mtime `/tmp/*.healthy`, docker healthcheck проверяет freshness (<30s). Liveness-only: для настоящего readiness надо двигать `beat()` в конец успешной бизнес-итерации.

### Structured JSON-логи

`logging_config.py` ставит JSON-формат на root-логер. Контекст прилетает через `extra={...}` — ruff-правило `G004` запрещает f-strings в логах, так что `payment_id`/`event_id` попадают отдельными полями, пригодными для агрегатора (ELK, Loki, Datadog).

## Тесты

```bash
pip install -e ".[dev]"
pytest
```

45 тестов, ~25 секунд:

- **unit** — Pydantic-валидация (amount cap, currency, HttpUrl), TokenBucket rate-limit (refill, concurrency), WebhookSender (2xx/4xx/5xx, network error, retry exhaustion)
- **integration** (`testcontainers[postgres]`) — `create_payment` с реальной БД: идемпотентность, outbox event создаётся один; `OutboxRelay._process_batch` — success/transient/exceed/FAILED-skipped
- **integration RMQ** (`testcontainers[rabbitmq]`) — DLX↔DLQ routing, declare queue arguments, declare идемпотентность
- **api** (FastAPI + ASGITransport + PG container) — полный HTTP-контракт: 202/200/401/400/422/429/404

## CI

`.github/workflows/ci.yml` гоняет на каждом PR и push в main три параллельных job'а:
- **lint** — `ruff check` + `ruff format --check`
- **typecheck** — `mypy src` (strict, `pydantic.mypy` plugin)
- **tests** — `pytest`

Та же комбинация доступна локально после `pip install -e ".[dev]"`.

## Разработка

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pre-commit install              # ruff + mypy

# Проверки (те же, что в CI)
ruff check src tests
ruff format --check src tests
mypy src
pytest

# Миграции (нужен запущенный Postgres)
alembic upgrade head
alembic revision --autogenerate -m "описание"

# Локально без Docker
uvicorn app.main:app --reload
python -m app.relay_main
python -m app.worker
```

## Структура

```
src/app/
  api/               FastAPI routes, deps (X-API-Key, Idempotency-Key), health, rate-limit
  broker/            RabbitMQ topology + DLQ monitor
  consumer/          payments.new handler
  models/            Payment, OutboxEvent (SQLAlchemy 2)
  outbox/            relay (polls pending, publishes, marks published/FAILED)
  schemas/           Pydantic v2 DTOs
  services/          create_payment (idempotency), get_payment
  webhook/           httpx + tenacity retry
  config.py          YAML → Pydantic Settings
  database.py        AsyncEngine + sessionmaker
  healthcheck.py     Heartbeat (touch-file для docker healthcheck)
  logging_config.py  JSON formatter + setup_logging
  main.py            FastAPI entrypoint
  relay_main.py      Relay + DLQ monitor entrypoint
  worker.py          Consumer entrypoint
migrations/          Alembic (async)
tests/               unit / integration / api
```
