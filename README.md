# Ош Базар — backend учёта электроэнергии

Серверная часть мобильного приложения учёта и оплаты электроэнергии Ошского базара:
показания счётчиков (с фото), тарифы, начисления, задолженность и аванс, оплата
через банк, электронные квитанции, уведомления, синхронизация с 1С и отчёты для
администрации.

Стек: **Django 5.2 + Django REST Framework**, аутентификация по ПИН-коду через
**JWT** (SimpleJWT), **PostgreSQL** (прод) / SQLite (dev), **OpenAPI** (drf-spectacular),
**django-storages/S3** для фото, **Gunicorn** + Docker.

---

## Быстрый старт (локально)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # заполните DJANGO_SECRET_KEY и пр.

python manage.py migrate
python manage.py seed_demo    # демо-данные и тестовые учётки
python manage.py runserver
```

Документация API (Swagger UI): http://127.0.0.1:8000/api/v1/docs/

Тестовые учётные записи после `seed_demo` (ПИН у всех `1234`):

| Роль | Телефон |
|------|---------|
| Супер-администратор | `+996700000040` |
| Администратор | `+996700000030` |
| Контролёр | `+996700000020` |
| Арендатор А-101 | `+996700000010` |
| Арендатор Б-202 | `+996700000011` |
| Арендатор В-303 | `+996700000012` |

## Запуск в Docker

```bash
cp .env.example .env
# минимум: DJANGO_SECRET_KEY, DB_PASSWORD
docker compose up --build
```

`docker compose` поднимает PostgreSQL и приложение (миграции и `collectstatic`
выполняются в `entrypoint.sh`). S3-совместимое хранилище (MinIO) для фото:

```bash
docker compose --profile s3 up --build
# затем в .env: USE_S3_STORAGE=true, AWS_S3_ENDPOINT_URL=http://minio:9000,
# AWS_STORAGE_BUCKET_NAME=osh-bazar, AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY
```

## Тесты

```bash
python manage.py test apps
python manage.py check
python manage.py spectacular --file schema.yml   # генерация OpenAPI
```

---

## Аутентификация и роли

Вход по **телефону + ПИН-коду** (`POST /api/v1/auth/login/`) → пара JWT
`access`/`refresh`. ПИН-код блокируется на 15 минут после 5 неверных попыток.

| Роль | Возможности |
|------|-------------|
| `tenant` (арендатор) | Свои точки, передача показаний, свои начисления, оплата, квитанции |
| `controller` (контролёр) | Просмотр и подтверждение/отклонение показаний |
| `admin` (администратор) | Управление точками, счётчиками, тарифами, периодами, платежами |
| `super_admin` | Полный доступ + синхронизация с 1С, интеграции |

Арендатор видит **только свои** торговые точки и связанные данные
(изоляция на уровне queryset).

## Соответствие ТЗ

| Раздел ТЗ | Реализация |
|-----------|------------|
| 1. Пользователи и роли | `apps/users`, `apps/trade_points` (M2M пользователь↔точка, RBAC) |
| 2. Справочники | `apps/trade_points` (точки, QR, счётчики), `apps/billing` (тарифы со снапшотом цены) |
| 3. Показания и медиа | `apps/readings` (валидация, фото, флоу approve/reject), S3 через `django-storages` |
| 4. Расчётный движок | `apps/billing` (потребление, начисления, долг, аванс, ledger, закрытие периода) |
| 5. Платежи | `apps/payments` (провайдер-абстракция, вебхуки с HMAC, идемпотентность, квитанции, возвраты) |
| 6. Интеграция с 1С | `apps/integrations` (клиент-абстракция, импорт арендаторов/точек/счётчиков, команда `sync_1c`) |
| 7. Уведомления | `apps/notifications` (push-бэкенды Logging/FCM/HTTP, device-токены, напоминания о долгах/сроках) |
| 8. Аналитика и логирование | `apps/reports` (dashboard, CSV-экспорт), audit-log, неизменяемый ledger |
| 9. Нефункциональные | PostgreSQL, Gunicorn, Docker, HTTPS-настройки, пагинация, троттлинг, изоляция данных |

## Ключевые эндпоинты

Префикс: `/api/v1/`. Полная схема — `schema.yml` или `/api/v1/docs/`.

- **Auth**: `auth/login/`, `auth/refresh/`, `auth/logout/`, `auth/verify/`, `me/`, `auth/pin/change/`
- **Точки**: `trade-points/`, `trade-points/{id}/qr/`, `trade-points/scan/{code}/`, `trade-points/{id}/summary/`, `meters/`
- **Показания**: `readings/`, `readings/preview/`, `readings/{id}/review/`
- **Биллинг**: `billing/periods/`, `billing/tariffs/`, `billing/charges/`, `billing/debt/`, `consumption/`
- **Платежи**: `payments/initiate/`, `payments/`, `payments/{id}/status/`, `payments/{id}/refund/`, `payments/webhook/{provider}/`, `receipts/`
- **Уведомления**: `notifications/`, `notifications/unread-count/`, `devices/`
- **Отчёты**: `reports/dashboard/`, `reports/export/{consumption,debts,payments}/`
- **Интеграции**: `integrations/status/`, `integrations/sync/`, `integrations/runs/`

Единый формат ошибок:

```json
{ "errors": [ { "code": "validation_error", "message": "…", "field": "amount" } ] }
```

## Структура проекта

```
config/            # настройки (base/dev/prod), urls, wsgi/asgi
apps/
  core/            # BaseModel, пагинация, исключения, permissions, audit, тексты, seed_demo
  users/           # кастомный User, JWT, ПИН, роли
  trade_points/    # точки, доступы, счётчики, QR/scan
  readings/        # показания и фото, подтверждение
  billing/         # периоды, тарифы, начисления, ledger, долг/аванс
  payments/        # провайдеры, вебхуки, квитанции
  notifications/   # уведомления, device-токены, push-бэкенды
  integrations/    # обмен с 1С
  reports/         # dashboard и CSV-экспорт
templates/         # печатная форма квитанции
locale/            # ru / ky
```

## Интеграции

- **Платежи.** Провайдер выбирается настройкой `PAYMENT_PROVIDERS`. В комплекте
  `mock` (песочница) и заготовка для реального банка (`BasePaymentProvider`,
  HMAC-SHA256 подпись `X-Signature`). Подключение банка — реализовать класс
  провайдера и прописать его путь в `PAYMENT_PROVIDERS`/`PAYMENT_PROVIDER_OPT_CLASS`.
- **1С.** `ONES_C_CLIENT` задаёт класс клиента; по умолчанию — `Http1CClient`.
  Импорт запускается командой `python manage.py sync_1c` или через API
  (`integrations/sync/`, только супер-админ).
- **Push.** `PUSH_BACKEND` выбирает бэкенд: `LoggingPushBackend` (по умолчанию),
  `FCMPushBackend` (FCM, `PUSH_API_KEY`) или `HTTPPushBackend` (свой шлюз,
  `PUSH_ENDPOINT`).
- **Фото → S3.** `USE_S3_STORAGE=true` + переменные `AWS_*` переключают хранилище
  медиа на S3/MinIO (`django-storages`).

## Продакшн

- `DJANGO_SETTINGS_MODULE=config.settings.prod`, непустой `DJANGO_SECRET_KEY`.
- PostgreSQL (`DB_ENGINE=django.db.backends.postgresql`), Gunicorn за reverse-proxy
  с HTTPS (`SECURE_SSL_REDIRECT=true`).
- Периодические задачи (команды под cron/systemd):
  `python manage.py notify_due`, `python manage.py notify_debtors`, `python manage.py sync_1c`.

## Конфигурация

Все переменные окружения описаны в [`.env.example`](.env.example).

## Локальный запуск после исправлений

`docker compose up -d --build` запускает API и PostgreSQL. Для Docker укажите
в `.env` `DB_NAME=osh_bazar`, `DB_USER=osh_bazar`, случайные `DB_PASSWORD`,
`DJANGO_SECRET_KEY`, `PAYMENT_WEBHOOK_SECRET` и `SECURE_SSL_REDIRECT=false`.
API: http://localhost:8000/api/v1/docs/, проверка: http://localhost:8000/health/.

Тестовые платежи отключены по умолчанию. Включайте `PAYMENT_SANDBOX_ENABLED=true`
только для демонстрации: mock-провайдер подтверждает платёж без реальных денег.
Фото показаний обязательно (`READING_PHOTO_REQUIRED=true`).
Проверки в контейнере: `docker compose exec web python manage.py test apps`.
Демо-данные создаются отдельно: `docker compose exec web python manage.py seed_demo`.

Без авторизации запрос восстановления ПИН принимается по `identifier` и
необязательному `comment` в `POST /api/v1/auth/pin-recovery/`. Ответ одинаковый
для существующего и неизвестного пользователя. Запрос не меняет ПИН:
администратор должен проверить личность заявителя перед одобрением.

По умолчанию Docker публикует API только на `127.0.0.1`. Для доступа с телефона
в локальной сети задайте `WEB_BIND_ADDRESS=0.0.0.0` и добавьте IP компьютера
в `DJANGO_ALLOWED_HOSTS`. Фотографии доступны через защищённый API:
`GET /api/v1/reading-photos/{id}/file/` с заголовком `Authorization: Bearer …`.

Для админ-панели по локальному HTTP также задайте `SESSION_COOKIE_SECURE=false`
и `CSRF_COOKIE_SECURE=false`. При публикации по HTTPS оба значения должны быть `true`.

Админ-панель оформлена через Jazzmin 3.0.5: `/admin/`. Название, навигация,
иконки и тема задаются в `JAZZMIN_SETTINGS` / `JAZZMIN_UI_TWEAKS` в `config/settings/base.py`.

## Vercel

Production: https://bazar.deo-core.codes/admin/ (проект `oshbazar-backend`).
PostgreSQL: отдельная база Neon `oshbazar-postgres`, бесплатный тариф, Frankfurt.
Фотографии: приватное хранилище Vercel Blob `oshbazar-photos`, доступ через API.
Демо-данные в облачную базу не загружаются; платежи mock отключены.

Vercel использует Python 3.13 и `config.wsgi:application`. Миграции выполняются
перед публикацией через `tool.vercel.scripts.build`; статические файлы собирает Vercel.
Зависимости в `pyproject.toml` должны соответствовать `requirements.txt`.

Production-переменные хранятся только в Vercel: `DATABASE_URL`,
`BLOB_READ_WRITE_TOKEN`, `DJANGO_SECRET_KEY`, `PAYMENT_WEBHOOK_SECRET`,
`DJANGO_SETTINGS_MODULE=config.settings.prod`, разрешённые домены и HTTPS-настройки.
`DATABASE_URL_UNPOOLED` можно использовать для ручных миграций.
Не подключайте preview-сборки к production-базе: настройте отдельную ветку Neon.
Локальные Docker PostgreSQL и фотографии остаются отдельными от облачных данных.
