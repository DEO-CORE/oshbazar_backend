"""Development settings. Used by default via manage.py."""
from .base import *  # noqa: F401,F403
from .base import DEBUG, env_bool

DEBUG = True

ALLOWED_HOSTS = ["*"]

CORS_ALLOW_ALL_ORIGINS = True

# Local file storage in development.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Sandbox bank provider always confirms payments in development.
PAYMENT_DEFAULT_PROVIDER = "mock"

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

LOGGING["root"]["level"] = "DEBUG"  # noqa: F405
if env_bool("DISABLE_FILE_LOGGING", False):
    LOGGING["root"]["handlers"] = ["console"]  # noqa: F405
    LOGGING["loggers"].pop("django.request", None)  # noqa: F405
    LOGGING["loggers"].pop("apps", None)  # noqa: F405
