"""Троттлинг публичных эндпоинтов приложения.

Авторизованные — по пользователю, анонимные — только по IP (device_id выбирает
клиент, поэтому ключом троттлинга он быть не может). Дополнительно действует
стандартный AnonRateThrottle проекта.
"""
from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle


class VersionCheckThrottle(SimpleRateThrottle):
    scope = "app_version_check"

    def get_rate(self):
        rates = getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_THROTTLE_RATES", {})
        return rates.get(self.scope) or getattr(settings, "APP_VERSION_CHECK_THROTTLE_RATE", "30/min")

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            ident = f"user:{request.user.pk}"
        else:
            ident = f"ip:{self.get_ident(request)}"
        return self.cache_format % {"scope": self.scope, "ident": ident}


class FeatureFlagThrottle(VersionCheckThrottle):
    scope = "app_features"
