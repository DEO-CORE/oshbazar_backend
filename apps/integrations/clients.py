"""Клиенты интеграции с 1С.

Реальный протокол (HTTP-сервисы 1С, OData, обмен файлами и т.п.) определяется
после получения информации о конфигурации 1С (ТЗ, п. 6, 25.3–25.5).
По умолчанию подключается заглушка `Stub1CClient`.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger("apps.integrations.ones")


class OneCError(Exception):
    pass


class Base1CClient(ABC):
    """Интерфейс обмена с 1С."""

    @abstractmethod
    def fetch_tenants(self) -> list[dict]:
        """Арендаторы из 1С."""

    @abstractmethod
    def fetch_trade_points(self) -> list[dict]:
        """Торговые точки с идентификаторами из 1С."""

    @abstractmethod
    def fetch_meters(self) -> list[dict]:
        """Электросчётчики торговых точек."""

    def push_documents(self, entity: str, documents: list[dict]) -> dict:
        """Передать документы (показания/начисления/платежи) обратно в 1С."""
        raise OneCError("Выгрузка в 1С не согласована с заказчиком.")


class Stub1CClient(Base1CClient):
    """Заглушка: интеграция не настроена. Возвращает пустые наборы."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def fetch_tenants(self) -> list[dict]:
        logger.info("1С интеграция не настроена: арендаторы не загружены")
        return []

    def fetch_trade_points(self) -> list[dict]:
        logger.info("1С интеграция не настроена: торговые точки не загружены")
        return []

    def fetch_meters(self) -> list[dict]:
        return []


class Http1CClient(Base1CClient):
    """Пример HTTP-клиента к сервисам 1С (конкретные эндпоинты задаются в settings)."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.base_url = (self.config.get("base_url") or getattr(settings, "ONES_C_BASE_URL", "")).rstrip("/")
        self.username = self.config.get("username") or getattr(settings, "ONES_C_USERNAME", "")
        self.password = self.config.get("password") or getattr(settings, "ONES_C_PASSWORD", "")
        self.timeout = int(self.config.get("timeout") or getattr(settings, "ONES_C_TIMEOUT", 30))

    def _request(self, path: str, method: str = "GET", payload: dict | None = None) -> dict:
        if not self.base_url:
            raise OneCError("Не задан ONES_C_BASE_URL.")
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        if self.username:
            import base64

            credentials = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            request.add_header("Authorization", f"Basic {credentials}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            raise OneCError(f"1С HTTP {exc.code}: {exc.reason}") from exc
        except Exception as exc:  # noqa: BLE001
            raise OneCError(f"Ошибка соединения с 1С: {exc}") from exc

    def fetch_tenants(self) -> list[dict]:
        payload = self._request(self.config.get("tenants_path", "tenants"))
        return payload.get("items", payload if isinstance(payload, list) else [])

    def fetch_trade_points(self) -> list[dict]:
        payload = self._request(self.config.get("trade_points_path", "trade-points"))
        return payload.get("items", payload if isinstance(payload, list) else [])

    def fetch_meters(self) -> list[dict]:
        payload = self._request(self.config.get("meters_path", "meters"))
        return payload.get("items", payload if isinstance(payload, list) else [])

    def push_documents(self, entity: str, documents: list[dict]) -> dict:
        return self._request(f"documents/{entity}", method="POST", payload={"items": documents})


def get_client() -> Base1CClient:
    dotted = getattr(settings, "ONES_C_CLIENT", "apps.integrations.clients.Http1CClient")
    config = getattr(settings, "ONES_C_CLIENT_CONFIG", {})
    return import_string(dotted)(config=config)
