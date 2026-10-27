"""Base settings for the "Ош Базар" electricity accounting backend.

All environment-specific settings live in `config.settings.dev` / `config.settings.prod`.
"""
import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", "insecure-dev-key-change-me-not-for-production-use-only")

DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    # Jazzmin must precede Django admin to override its templates.
    "jazzmin",
    # Django
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # Third party
    "rest_framework",
    "rest_framework.authtoken",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
    "storages",
    # Local apps
    "apps.core",
    "apps.users",
    "apps.trade_points",
    "apps.readings",
    "apps.billing",
    "apps.payments",
    "apps.notifications",
    "apps.integrations",
    "apps.reports",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.RequestContextMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

AUTH_USER_MODEL = "users.User"

# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": env("DB_ENGINE", "django.db.backends.sqlite3"),
        "NAME": env("DB_NAME", str(BASE_DIR / "db.sqlite3")),
        "USER": env("DB_USER", ""),
        "PASSWORD": env("DB_PASSWORD", ""),
        "HOST": env("DB_HOST", ""),
        "PORT": env("DB_PORT", ""),
        "CONN_MAX_AGE": int(env("DB_CONN_MAX_AGE", "60")),
        "OPTIONS": {"charset": "utf8"} if env("DB_ENGINE", "").endswith("mysql") else {},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Internationalisation (ru / ky)
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "ru"

TIME_ZONE = env("DJANGO_TIME_ZONE", "Asia/Bishkek")

USE_I18N = True

USE_TZ = True

LANGUAGES = [
    ("ru", "Русский"),
    ("ky", "Кыргызча"),
]

LOCALE_PATHS = [BASE_DIR / "locale"]

# ---------------------------------------------------------------------------
# Static & media
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / env("MEDIA_ROOT_NAME", "media")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# REST framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.DefaultPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "URL_FORMAT_OVERRIDE": None,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "200/min",
        "user": "600/min",
        "auth_pin": "10/min",
    },
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
    "DATETIME_FORMAT": "%Y-%m-%dT%H:%M:%S%z",
    "DATE_FORMAT": "%Y-%m-%d",
}

# ---------------------------------------------------------------------------
# Simple JWT
# ---------------------------------------------------------------------------
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(env("JWT_ACCESS_TTL_MINUTES", "30"))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(env("JWT_REFRESH_TTL_DAYS", "14"))),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": (env("JWT_SIGNING_KEY") or SECRET_KEY),
    "VERIFYING_KEY": None,
    "AUDIENCE": None,
    "ISSUER": None,
    "LEEWAY": 0,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_HEADER_NAME": "HTTP_AUTHORIZATION",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "USER_AUTHENTICATION_RULE": "rest_framework_simplejwt.authentication.default_user_authentication_rule",
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "TOKEN_TYPE_CLAIM": "token_type",
    "JTI_CLAIM": "jti",
}

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = env_bool("CORS_ALLOW_ALL_ORIGINS", DEBUG)
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "")

# ---------------------------------------------------------------------------
# Application specific settings
# ---------------------------------------------------------------------------
APP_NAME = env("APP_NAME", "Ош Базар — учёт электроэнергии")

# Reading validation: reject a value lower than the previous one.
READINGS_ALLOW_DECREASE = env_bool("READINGS_ALLOW_DECREASE", False)
# Maximum photo size in megabytes.
READING_PHOTO_MAX_MB = int(env("READING_PHOTO_MAX_MB", "10"))
READING_PHOTO_ALLOWED_TYPES = env_list(
    "READING_PHOTO_ALLOWED_TYPES", "image/jpeg,image/png,image/webp"
)

# Payment providers: python path of the provider class keyed by provider code.
PAYMENT_PROVIDERS = {
    "mock": "apps.payments.providers.mock.MockBankProvider",
    "opt": env("PAYMENT_PROVIDER_OPT_CLASS", "apps.payments.providers.mock.MockBankProvider"),
}
PAYMENT_DEFAULT_PROVIDER = env("PAYMENT_DEFAULT_PROVIDER", "mock")
PAYMENT_CURRENCY = env("PAYMENT_CURRENCY", "KGS")
PAYMENT_WEBHOOK_SECRET = env("PAYMENT_WEBHOOK_SECRET", "change-me-webhook-secret")

# Days before the due date when the "approaching payment" reminder is sent.
PAYMENT_DUE_REMINDER_DAYS = int(env("PAYMENT_DUE_REMINDER_DAYS", "3"))
# Default number of days allowed to pay a charge.
PAYMENT_DUE_DAYS = int(env("PAYMENT_DUE_DAYS", "10"))

# 1C integration (protocol/endpoints are to be confirmed with the customer).
ONES_C_ENABLED = env_bool("ONES_C_ENABLED", False)
ONES_C_BASE_URL = env("ONES_C_BASE_URL", "")
ONES_C_USERNAME = env("ONES_C_USERNAME", "")
ONES_C_PASSWORD = env("ONES_C_PASSWORD", "")
ONES_C_TIMEOUT = int(env("ONES_C_TIMEOUT", "30"))

# Push notifications (FCM / similar). Kept abstract — vendor is TBC.
PUSH_ENABLED = env_bool("PUSH_ENABLED", False)
PUSH_API_KEY = env("PUSH_API_KEY", "")

SPECTACULAR_SETTINGS = {
    "TITLE": "Ош Базар — API учёта электроэнергии",
    "DESCRIPTION": (
        "Backend для мобильного приложения учёта и оплаты электроэнергии "
        "Ошского базара: показания счётчиков, тарифы, начисления, задолженность, "
        "банковская оплата, квитанции и уведомления."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "TAGS": [
        {"name": "auth", "description": "Авторизация по ПИН-коду (JWT)"},
        {"name": "profile", "description": "Профиль пользователя и устройства"},
        {"name": "trade-points", "description": "Торговые точки, счётчики, QR"},
        {"name": "readings", "description": "Передача показаний электросчётчиков"},
        {"name": "consumption", "description": "История потребления и графики"},
        {"name": "billing", "description": "Периоды, тарифы, начисления, задолженность"},
        {"name": "payments", "description": "Банковская оплата и вебхуки"},
        {"name": "receipts", "description": "Электронные квитанции"},
        {"name": "notifications", "description": "Push-уведомления"},
        {"name": "staff", "description": "Контролёр / администратор / супер-администратор"},
        {"name": "reports", "description": "Отчёты и экспорт"},
        {"name": "integrations", "description": "Интеграция с 1С"},
    ],
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "%(levelname)s %(asctime)s %(name)s %(message)s"},
        "simple": {"format": "%(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "django.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "verbose",
            "encoding": "utf-8",
        },
    },
    "root": {"handlers": ["console", "file"], "level": "INFO"},
    "loggers": {
        "django.request": {"level": "WARNING", "handlers": ["console", "file"], "propagate": False},
        "apps": {"level": "INFO", "handlers": ["console", "file"], "propagate": False},
    },
}


# Reading submission flow (ТЗ, п. 25.14 — подтверждение показаний согласуется с заказчиком).
READINGS_REQUIRE_APPROVAL = env_bool("READINGS_REQUIRE_APPROVAL", False)
# Automatically offset new charges with a positive user balance (ТЗ, п. 12).
AUTO_APPLY_CREDIT = env_bool("AUTO_APPLY_CREDIT", True)

# Receipts
RECEIPT_PREFIX = env("RECEIPT_PREFIX", "OB")

# Push backend (vendor is TBC). Default writes pushes to the application log.
PUSH_BACKEND = env("PUSH_BACKEND", "apps.notifications.push.LoggingPushBackend")
# Universal HTTP gateway (HTTPPushBackend) and FCM (FCMPushBackend) options.
PUSH_ENDPOINT = env("PUSH_ENDPOINT", "")
PUSH_FCM_ENDPOINT = env("PUSH_FCM_ENDPOINT", "")
PUSH_AUTH_HEADER = env("PUSH_AUTH_HEADER", "Authorization")
PUSH_TIMEOUT = int(env("PUSH_TIMEOUT", "10"))

# 1C client implementation and its per-instance configuration.
ONES_C_CLIENT = env("ONES_C_CLIENT", "apps.integrations.clients.Http1CClient")
ONES_C_CLIENT_CONFIG = {}
PAYMENT_PROVIDERS_CONFIG = {}

# Reading photo requirement (ТЗ, п. 21: фото — подтверждение показания).
READING_PHOTO_REQUIRED = env_bool("READING_PHOTO_REQUIRED", True)
PAYMENT_SANDBOX_ENABLED = env_bool("PAYMENT_SANDBOX_ENABLED", DEBUG)


# Administration interface
JAZZMIN_SETTINGS = {
    "site_title": "Ош Базар — администрирование",
    "site_header": "Ош Базар",
    "site_brand": "Ош Базар",
    "welcome_sign": "Управление учётом электроэнергии",
    "copyright": "Ош Базар",
    "show_sidebar": True,
    "navigation_expanded": True,
    "show_ui_builder": False,
    "search_model": ["trade_points.TradePoint", "users.User"],
    "user_avatar": None,
    "use_google_fonts_cdn": False,
    "order_with_respect_to": [
        "trade_points", "readings", "billing", "payments", "users",
        "notifications", "integrations", "auth",
    ],
    "topmenu_links": [
        {"name": "Главная", "url": "admin:index", "permissions": ["auth.view_permission"]},
        {"name": "Документация API", "url": "/api/v1/docs/", "new_window": True},
    ],
    "icons": {
        "auth": "fas fa-shield-alt",
        "auth.Group": "fas fa-users-cog",
        "users.User": "fas fa-user",
        "users.PinRecoveryRequest": "fas fa-key",
        "trade_points.TradePoint": "fas fa-store",
        "trade_points.TradePointAccess": "fas fa-user-lock",
        "trade_points.Meter": "fas fa-tachometer-alt",
        "readings.MeterReading": "fas fa-bolt",
        "readings.ReadingPhoto": "fas fa-camera",
        "billing.BillingPeriod": "fas fa-calendar-alt",
        "billing.Tariff": "fas fa-tags",
        "billing.Charge": "fas fa-file-invoice-dollar",
        "billing.LedgerEntry": "fas fa-book",
        "payments.Payment": "fas fa-credit-card",
        "payments.PaymentAllocation": "fas fa-exchange-alt",
        "payments.Receipt": "fas fa-receipt",
        "payments.BankWebhookLog": "fas fa-history",
        "notifications.Notification": "fas fa-bell",
        "notifications.DeviceToken": "fas fa-mobile-alt",
        "integrations.SyncRun": "fas fa-sync-alt",
        "integrations.ExternalObject": "fas fa-link",
    },
    "default_icon_parents": "fas fa-folder",
    "default_icon_children": "fas fa-circle",
    "changeform_format": "horizontal_tabs",
    "related_modal_active": False,
}

JAZZMIN_UI_TWEAKS = {
    "theme": "flatly",
    "navbar": "navbar-white navbar-light",
    "sidebar": "sidebar-dark-primary",
    "brand_colour": "navbar-primary",
    "accent": "accent-primary",
    "sidebar_nav_compact_style": True,
    "footer_fixed": False,
    "actions_sticky_top": True,
}
