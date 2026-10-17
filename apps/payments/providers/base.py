"""Абстракция банковского платёжного провайдера.

Конкретный банк и его API определяются заказчиком (ТЗ, п. 13, 25.20).
Интеграция подключается реализацией `BasePaymentProvider` и регистрацией
класса в настройке `PAYMENT_PROVIDERS`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderInitResult:
    """Ответ банка на создание платежа."""

    status: str = "pending"
    provider_payment_id: str = ""
    redirect_url: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderCallback:
    """Нормализованное уведомление банка о результате платежа."""

    payment_id: str = ""
    provider_payment_id: str = ""
    status: str = "failed"
    amount: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class BasePaymentProvider(ABC):
    code: str = "base"

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    # --- lifecycle ---------------------------------------------------------
    @abstractmethod
    def create_payment(self, payment, request=None) -> ProviderInitResult:
        """Инициировать платёж в банке и получить ссылку на оплату."""

    @abstractmethod
    def parse_callback(self, payload: dict, headers: dict | None = None) -> ProviderCallback:
        """Разобрать уведомление банка в общий формат."""

    def verify_signature(self, raw_body: bytes | str, headers: dict | None = None) -> bool:
        """Проверка подписи вебхука. По умолчанию — HMAC-SHA256 в заголовке."""
        from django.conf import settings

        headers = headers or {}
        secret = self.config.get("webhook_secret") or getattr(
            settings, "PAYMENT_WEBHOOK_SECRET", ""
        )
        if not secret:
            return False
        signature = (
            headers.get("X-Signature")
            or headers.get("x-signature")
            or headers.get("HTTP_X_SIGNATURE")
            or ""
        )
        expected = compute_signature(raw_body, secret)
        return hmac.compare_digest(expected, signature.strip())

    @staticmethod
    def normalize_status(raw_status: str) -> str:
        mapping = {
            "success": "succeeded",
            "succeeded": "succeeded",
            "paid": "succeeded",
            "completed": "succeeded",
            "approved": "succeeded",
            "failed": "failed",
            "declined": "failed",
            "error": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "refunded": "refunded",
            "processing": "processing",
            "pending": "pending",
        }
        return mapping.get((raw_status or "").strip().lower(), "failed")


def compute_signature(raw_body: bytes | str, secret: str) -> str:
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def sign_payload(payload: dict, secret: str) -> str:
    """Утилита для тестов и документации: подпись тела запроса."""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return compute_signature(body, secret)
