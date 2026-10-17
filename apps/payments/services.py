"""Платёжный сервис: инициирование, вебхуки банка, квитанции."""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.services import allocate_amount, money

from .models import (
    BankWebhookLog,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    Receipt,
)
from .providers.base import compute_signature
from .providers.registry import get_provider

logger = logging.getLogger("apps.payments")


class PaymentError(Exception):
    """Ошибка платёжного сценария с машиночитаемым кодом."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Инициирование
# ---------------------------------------------------------------------------
def initiate_payment(
    *,
    user,
    amount,
    provider_code: str | None = None,
    trade_point=None,
    charge_ids: list | None = None,
    description: str = "",
    idempotency_key: str | None = None,
    request=None,
) -> Payment:
    amount = money(amount)
    if amount <= 0:
        raise PaymentError("invalid_amount", "Сумма платежа должна быть больше нуля.")

    if idempotency_key:
        existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing

    provider = get_provider(provider_code)
    currency = getattr(settings, "PAYMENT_CURRENCY", "KGS")

    payment = Payment.objects.create(
        user=user,
        trade_point=trade_point,
        amount=amount,
        currency=currency,
        provider=provider.code,
        description=description or "Оплата электроэнергии",
        idempotency_key=idempotency_key,
        target_charge_ids=[str(cid) for cid in (charge_ids or [])],
    )

    try:
        result = provider.create_payment(payment, request=request)
    except Exception as exc:  # noqa: BLE001 — ошибки банка логируем и сообщаем клиенту
        logger.exception("Payment provider %s error", provider.code)
        payment.status = PaymentStatus.FAILED
        payment.failure_reason = str(exc)[:255]
        payment.save(update_fields=["status", "failure_reason", "updated_at"])
        raise PaymentError("provider_error", "Банк недоступен, попробуйте позже.") from exc

    payment.provider_payment_id = result.provider_payment_id or payment.provider_payment_id
    payment.redirect_url = result.redirect_url or ""
    payment.raw_payload = result.payload or {}
    payment.save(
        update_fields=["provider_payment_id", "redirect_url", "raw_payload", "updated_at"]
    )

    if result.status == "succeeded":
        # complete_payment сам выставит SUCCEEDED и сформирует квитанцию.
        complete_payment(payment)
        payment.refresh_from_db()

    return payment


# ---------------------------------------------------------------------------
# Завершение / отмена
# ---------------------------------------------------------------------------
@transaction.atomic
def complete_payment(
    payment: Payment,
    *,
    provider_payment_id: str = "",
    callback_amount: Decimal | str | None = None,
    payload: dict | None = None,
) -> Payment:
    """Провести успешный платёж (идемпотентно) и сформировать квитанцию."""
    payment = Payment.objects.select_for_update().get(pk=payment.pk)

    if payment.status == PaymentStatus.SUCCEEDED:
        return payment

    if callback_amount is not None and money(callback_amount) != payment.amount:
        payment.status = PaymentStatus.FAILED
        payment.failure_reason = "Сумма в уведомлении банка не совпадает с суммой платежа."
        payment.raw_payload = payload or payment.raw_payload
        payment.save(update_fields=["status", "failure_reason", "raw_payload", "updated_at"])
        _notify_failed(payment)
        return payment

    if provider_payment_id:
        payment.provider_payment_id = provider_payment_id
    if payload:
        payment.raw_payload = payload
    payment.status = PaymentStatus.SUCCEEDED
    payment.completed_at = timezone.now()
    payment.save(
        update_fields=["provider_payment_id", "raw_payload", "status", "completed_at", "updated_at"]
    )

    charge_ids = payment.target_charge_ids or None
    allocate_amount(
        payment.user,
        payment.amount,
        payment=payment,
        trade_point=payment.trade_point,
        charge_ids=charge_ids,
    )

    create_receipt(payment)
    _notify_success(payment)
    return payment


@transaction.atomic
def fail_payment(payment: Payment, *, status: str = PaymentStatus.FAILED, reason: str = "",
                 payload: dict | None = None) -> Payment:
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status == PaymentStatus.SUCCEEDED:
        return payment
    payment.status = status
    payment.failure_reason = reason[:255]
    if payload:
        payment.raw_payload = payload
    payment.save(update_fields=["status", "failure_reason", "raw_payload", "updated_at"])
    _notify_failed(payment)
    return payment


def refund_payment_record(payment: Payment, *, reason: str = "") -> dict:
    from apps.billing.services import refund_payment

    return refund_payment(payment, reason=reason)


# ---------------------------------------------------------------------------
# Вебхуки
# ---------------------------------------------------------------------------
@transaction.atomic
def process_webhook(provider_code: str, payload: dict, headers: dict, raw_body: bytes) -> dict:
    provider = get_provider(provider_code)
    headers = headers or {}
    signature = headers.get("X-Signature") or headers.get("HTTP_X_SIGNATURE") or ""

    secret = getattr(settings, "PAYMENT_WEBHOOK_SECRET", "")
    expected = compute_signature(raw_body, secret) if secret else ""
    signature_valid = bool(expected) and expected == signature

    log = BankWebhookLog.objects.create(
        provider=provider_code,
        payload=payload,
        signature=signature,
        signature_valid=signature_valid,
        external_id=str(payload.get("id") or payload.get("provider_payment_id") or ""),
    )

    if not signature_valid:
        if not provider.verify_signature(raw_body, headers):
            log.error = "invalid_signature"
            log.save(update_fields=["error", "updated_at"])
            raise PaymentError("invalid_signature", "Неверная подпись уведомления банка.")

    callback = provider.parse_callback(payload, headers)

    payment = None
    if callback.provider_payment_id:
        payment = Payment.objects.filter(provider_payment_id=callback.provider_payment_id).first()
    if payment is None and callback.payment_id:
        payment = Payment.objects.filter(pk=callback.payment_id).first() if _is_uuid(callback.payment_id) else None
    if payment is None:
        log.error = "payment_not_found"
        log.save(update_fields=["error", "updated_at"])
        raise PaymentError("payment_not_found", "Платёж не найден.")

    if payment.status == PaymentStatus.SUCCEEDED and callback.status == "succeeded":
        log.processed = True
        log.save(update_fields=["processed", "updated_at"])
        return {"status": "already_processed", "payment_id": str(payment.id)}

    if callback.status == "succeeded":
        complete_payment(
            payment,
            provider_payment_id=callback.provider_payment_id,
            callback_amount=callback.amount,
            payload=payload,
        )
    elif callback.status in {"failed", "cancelled"}:
        fail_payment(
            payment,
            status=PaymentStatus.FAILED if callback.status == "failed" else PaymentStatus.CANCELLED,
            reason=f"Банк вернул статус: {callback.status}",
            payload=payload,
        )
    else:
        payment.status = PaymentStatus.PROCESSING
        payment.raw_payload = payload
        payment.save(update_fields=["status", "raw_payload", "updated_at"])

    log.processed = True
    log.save(update_fields=["processed", "updated_at"])
    payment.refresh_from_db()
    return {"status": "ok", "payment_id": str(payment.id), "payment_status": payment.status}


def _is_uuid(value: str) -> bool:
    import uuid as _uuid

    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Квитанции
# ---------------------------------------------------------------------------
def generate_receipt_number(payment: Payment) -> str:
    prefix = getattr(settings, "RECEIPT_PREFIX", "OB")
    return f"{prefix}-{timezone.localdate():%Y%m}-{payment.id.hex[:8].upper()}"


def create_receipt(payment: Payment) -> Receipt:
    receipt, created = Receipt.objects.get_or_create(
        payment=payment,
        defaults={"number": generate_receipt_number(payment), "data": {}},
    )
    data = receipt_data(payment)
    receipt.data = data
    receipt.save(update_fields=["data", "updated_at"])
    return receipt


def receipt_data(payment: Payment) -> dict:
    allocations = (
        PaymentAllocation.objects.filter(payment=payment)
        .select_related("charge", "charge__period", "charge__trade_point", "charge__tariff")
        .order_by("charge__period__year", "charge__period__month")
    )
    charges = []
    for allocation in allocations:
        charge = allocation.charge
        charges.append(
            {
                "charge_id": str(charge.id),
                "period": charge.period.label,
                "period_title": charge.period.title,
                "trade_point": charge.trade_point.code,
                "trade_point_name": charge.trade_point.name,
                "consumption": str(charge.consumption),
                "tariff": str(charge.tariff_price),
                "charged": str(charge.amount),
                "allocated": str(allocation.amount),
            }
        )
    return {
        "number": generate_receipt_number(payment),
        "issued_at": timezone.now().isoformat(),
        "payment_id": str(payment.id),
        "provider": payment.provider,
        "provider_payment_id": payment.provider_payment_id,
        "amount": str(payment.amount),
        "currency": payment.currency,
        "status": payment.status,
        "payer": {
            "id": str(payment.user_id),
            "name": str(payment.user),
            "phone": getattr(payment.user, "phone", ""),
        },
        "trade_point": (
            {"id": str(payment.trade_point_id), "code": payment.trade_point.code,
             "name": payment.trade_point.name}
            if payment.trade_point
            else None
        ),
        "charges": charges,
    }


# ---------------------------------------------------------------------------
# Уведомления
# ---------------------------------------------------------------------------
def _notify_success(payment: Payment) -> None:
    from apps.notifications.services import notify

    notify(
        payment.user,
        "payment_success",
        {"amount": f"{payment.amount:.2f}", "currency": payment.currency},
    )


def _notify_failed(payment: Payment) -> None:
    from apps.notifications.services import notify

    notify(
        payment.user,
        "payment_failed",
        {"amount": f"{payment.amount:.2f}", "currency": payment.currency},
    )
