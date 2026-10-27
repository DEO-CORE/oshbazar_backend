"""Production settings. Set DJANGO_SETTINGS_MODULE=config.settings.prod."""
from .base import *  # noqa: F401,F403
from .base import DEBUG, SECRET_KEY, env, env_bool, env_list

DEBUG = env_bool("DJANGO_DEBUG", False)

if not DEBUG and SECRET_KEY.startswith("insecure"):
    raise RuntimeError("DJANGO_SECRET_KEY must be set in production")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", True)
CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", True)
SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "")
CORS_ALLOW_CREDENTIALS = True

STORAGES = {
    "default": {"BACKEND": env("STORAGES_DEFAULT_BACKEND", "django.core.files.storage.FileSystemStorage")},
    "staticfiles": {
        "BACKEND": env(
            "STORAGES_STATICFILES_BACKEND",
            "apps.core.storage.AdminStaticFilesStorage",
        )
    },
}

# Database driver must be selected explicitly in production.
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3" and env_bool(  # noqa: F405
    "REQUIRE_POSTGRES", True
):
    raise RuntimeError("Set DB_ENGINE=django.db.backends.postgresql for production")

if env_bool("USE_S3_STORAGE", False):
    s3_options = {
        "bucket_name": env("AWS_STORAGE_BUCKET_NAME", ""),
        "region_name": env("AWS_S3_REGION_NAME", ""),
        "access_key": env("AWS_ACCESS_KEY_ID", ""),
        "secret_key": env("AWS_SECRET_ACCESS_KEY", ""),
        # S3-compatible endpoints (MinIO, Selectel, Yandex Object Storage и т.п.).
        "endpoint_url": env("AWS_S3_ENDPOINT_URL", "") or None,
        "custom_domain": env("AWS_S3_CUSTOM_DOMAIN", "") or None,
        "querystring_auth": env_bool("AWS_QUERYSTRING_AUTH", True),
        "signature_version": env("AWS_S3_SIGNATURE_VERSION", "s3v4"),
        # Never overwrite a photo that resolves to the same key.
        "file_overwrite": False,
        "default_acl": env("AWS_DEFAULT_ACL", "") or None,
        "url_protocol": env("AWS_S3_URL_PROTOCOL", "https:"),
    }
    STORAGES["default"] = {  # noqa: F405
        "BACKEND": env("STORAGES_DEFAULT_BACKEND", "storages.backends.s3boto3.S3Boto3Storage"),
        "OPTIONS": {k: v for k, v in s3_options.items() if v not in (None, "")},
    }

if env("BLOB_READ_WRITE_TOKEN"):
    STORAGES["default"] = {"BACKEND": "apps.core.storage.VercelBlobStorage"}

if env("VERCEL"):
    # Vercel moves collected assets to its CDN and omits STATIC_ROOT from
    # the function bundle. Use stable CDN paths, without a runtime manifest.
    STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
    MIDDLEWARE = [item for item in MIDDLEWARE if item != "whitenoise.middleware.WhiteNoiseMiddleware"]
    # Select WSGI explicitly, matching pyproject.toml and the function config.
    del ASGI_APPLICATION
