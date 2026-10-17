"""Реестр платёжных провайдеров."""
from functools import lru_cache

from django.conf import settings
from django.utils.module_loading import import_string

from .base import BasePaymentProvider


def available_providers() -> dict[str, str]:
    return dict(getattr(settings, "PAYMENT_PROVIDERS", {}))


@lru_cache(maxsize=32)
def _load(code: str) -> type[BasePaymentProvider]:
    dotted = available_providers().get(code)
    if not dotted:
        raise ValueError(f"Платёжный провайдер '{code}' не настроен.")
    return import_string(dotted)


def get_provider(code: str | None = None) -> BasePaymentProvider:
    code = code or getattr(settings, "PAYMENT_DEFAULT_PROVIDER", "mock")
    cls = _load(code)
    config = {}
    provider_settings = getattr(settings, "PAYMENT_PROVIDERS_CONFIG", {})
    config.update(provider_settings.get(code, {}))
    return cls(config=config)


def default_provider_code() -> str:
    return getattr(settings, "PAYMENT_DEFAULT_PROVIDER", "mock")
