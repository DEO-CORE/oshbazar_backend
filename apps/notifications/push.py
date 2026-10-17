"""Push-бэкенды.

Вендор push-провайдера определяется заказчиком (ТЗ, п. 17). По умолчанию
используется `LoggingPushBackend` (пишет в лог). Для реальной доставки
предусмотрены:

* `FCMPushBackend` — Firebase Cloud Messaging (HTTP API, серверный ключ);
* `HTTPPushBackend` — универсальный JSON-POST на произвольный эндпоинт
  (релей, собственный шлюз, другой провайдер).

Бэкенд выбирается настройкой `PUSH_BACKEND` (python-путь класса).
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger("apps.notifications.push")


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> tuple[bool, dict | str]:
    """Отправить JSON-POST. Вернуть (успех, разобранный_ответ | текст_ошибки)."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            ok = 200 <= response.status < 300
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = raw
            return ok, parsed
    except urllib.error.HTTPError as exc:  # 4xx/5xx
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        logger.warning("PUSH HTTP %s: %s", exc.code, detail[:300])
        return False, detail[:300]
    except urllib.error.URLError as exc:
        logger.warning("PUSH недоступен: %s", exc.reason)
        return False, str(exc.reason)
    except TimeoutError:
        logger.warning("PUSH таймаут запроса к %s", url)
        return False, "timeout"


class BasePushBackend(ABC):
    code = "base"

    @abstractmethod
    def send(self, *, token: str, title: str, body: str, data: dict | None = None) -> bool:
        """Отправить push на конкретный токен. Вернуть True при успехе."""


class LoggingPushBackend(BasePushBackend):
    """Заглушка: пишет push в лог. Используется до выбора push-провайдера."""

    code = "logging"

    def send(self, *, token: str, title: str, body: str, data: dict | None = None) -> bool:
        logger.info("PUSH → %s | %s | %s | %s", token[:24], title, body, data or {})
        return True


class FCMPushBackend(BasePushBackend):
    """Firebase Cloud Messaging (HTTP API, серверный ключ `PUSH_API_KEY`).

    Легаси-эндпоинт FCM ожидает `Authorization: key=<server_key>`. Эндпоинт
    можно переопределить настройкой `PUSH_FCM_ENDPOINT` (релей / HTTP v1 прокси).
    """

    code = "fcm"
    default_endpoint = "https://fcm.googleapis.com/fcm/send"

    def send(self, *, token: str, title: str, body: str, data: dict | None = None) -> bool:
        api_key = getattr(settings, "PUSH_API_KEY", "")
        if not api_key:
            logger.warning("PUSH: PUSH_API_KEY не задан, уведомление не отправлено")
            return False

        endpoint = getattr(settings, "PUSH_FCM_ENDPOINT", "") or self.default_endpoint
        timeout = float(getattr(settings, "PUSH_TIMEOUT", 10))
        payload = {
            "to": token,
            "notification": {"title": title, "body": body},
            "data": {k: str(v) for k, v in (data or {}).items()},
            "priority": "high",
        }
        headers = {
            "Authorization": f"key={api_key}",
            "Content-Type": "application/json",
        }
        ok, result = _post_json(endpoint, payload, headers, timeout)
        if not ok:
            return False
        if isinstance(result, dict):
            # Легаси-ответ FCM: {"success": 1, "failure": 0, "results": [...]}.
            if "failure" in result or "success" in result:
                return int(result.get("success", 0)) >= 1
            if "results" in result:
                return all("error" not in item for item in result["results"])
        return True


class HTTPPushBackend(BasePushBackend):
    """Универсальный JSON-POST на произвольный шлюз.

    Настройки:
        PUSH_ENDPOINT   — URL шлюза (обязательно);
        PUSH_API_KEY    — значение авторизации (опционально);
        PUSH_AUTH_HEADER— имя заголовка авторизации (по умолчанию Authorization).
    """

    code = "http"

    def send(self, *, token: str, title: str, body: str, data: dict | None = None) -> bool:
        endpoint = getattr(settings, "PUSH_ENDPOINT", "")
        if not endpoint:
            logger.warning("PUSH: PUSH_ENDPOINT не задан, уведомление не отправлено")
            return False

        timeout = float(getattr(settings, "PUSH_TIMEOUT", 10))
        payload = {
            "token": token,
            "title": title,
            "body": body,
            "data": data or {},
        }
        headers = {"Content-Type": "application/json"}
        api_key = getattr(settings, "PUSH_API_KEY", "")
        if api_key:
            header_name = getattr(settings, "PUSH_AUTH_HEADER", "Authorization")
            headers[header_name] = api_key

        ok, result = _post_json(endpoint, payload, headers, timeout)
        if not ok:
            return False
        if isinstance(result, dict) and "success" in result:
            return bool(result["success"])
        return True


def get_push_backend() -> BasePushBackend:
    dotted = getattr(settings, "PUSH_BACKEND", "apps.notifications.push.LoggingPushBackend")
    return import_string(dotted)()
