"""Песочница платёжного провайдера (dev/test).

Имитирует банк: создаёт «ссылку на оплату» и мгновенно подтверждает платёж.
Используется до подключения реального банковского API (ТЗ, п. 13).
"""
from __future__ import annotations

import uuid

from .base import BasePaymentProvider, ProviderCallback, ProviderInitResult


class MockBankProvider(BasePaymentProvider):
    code = "mock"

    def create_payment(self, payment, request=None) -> ProviderInitResult:
        from django.conf import settings
        from apps.payments.services import PaymentError

        if not getattr(settings, "PAYMENT_SANDBOX_ENABLED", False):
            raise PaymentError("sandbox_disabled", "Тестовые платежи отключены.")
        provider_payment_id = f"MOCK-{uuid.uuid4().hex[:12].upper()}"
        base = ""
        if request is not None:
            base = request.build_absolute_uri("/").rstrip("/")
        redirect_url = self.config.get("redirect_url") or (
            f"{base}/api/v1/payments/mock/checkout/{provider_payment_id}/"
        )
        auto_confirm = self.config.get("auto_confirm", True)
        return ProviderInitResult(
            status="succeeded" if auto_confirm else "pending",
            provider_payment_id=provider_payment_id,
            redirect_url=redirect_url,
            payload={"sandbox": True, "auto_confirm": auto_confirm},
        )

    def parse_callback(self, payload: dict, headers: dict | None = None) -> ProviderCallback:
        return ProviderCallback(
            payment_id=str(payload.get("payment_id") or payload.get("order_id") or ""),
            provider_payment_id=str(payload.get("provider_payment_id") or payload.get("id") or ""),
            status=self.normalize_status(payload.get("status", "")),
            amount=str(payload["amount"]) if payload.get("amount") is not None else None,
            payload=payload,
        )

    def verify_signature(self, raw_body, headers=None) -> bool:
        """В песочнице подпись проверяется, но допускается отключение в DEBUG."""
        from django.conf import settings

        if getattr(settings, "DEBUG", False) and self.config.get("skip_signature"):
            return True
        return super().verify_signature(raw_body, headers)
