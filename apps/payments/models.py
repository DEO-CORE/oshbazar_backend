"""Платежи, распределение по начислениям, квитанции и журнал вебхуков банка."""
import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel


class PaymentStatus(models.TextChoices):
    PENDING = "pending", _("ожидает оплаты")
    PROCESSING = "processing", _("обрабатывается банком")
    SUCCEEDED = "succeeded", _("успешно")
    FAILED = "failed", _("ошибка")
    CANCELLED = "cancelled", _("отменён")
    REFUNDED = "refunded", _("возврат")


class Payment(BaseModel):
    """Платёж через банковскую систему (ТЗ, п. 13)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="payments",
        verbose_name=_("плательщик"),
    )
    trade_point = models.ForeignKey(
        "trade_points.TradePoint",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments",
        verbose_name=_("торговая точка"),
    )

    amount = models.DecimalField(_("сумма"), max_digits=12, decimal_places=2)
    currency = models.CharField(_("валюта"), max_length=8, default="KGS")
    status = models.CharField(
        _("статус"), max_length=16, choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING, db_index=True,
    )
    provider = models.CharField(_("банк / провайдер"), max_length=32, db_index=True)
    provider_payment_id = models.CharField(
        _("ID платежа в банке"), max_length=128, blank=True, db_index=True
    )
    redirect_url = models.URLField(_("ссылка на оплату"), max_length=1000, blank=True)
    idempotency_key = models.CharField(
        _("ключ идемпотентности"), max_length=128, unique=True, null=True, blank=True
    )
    description = models.CharField(_("назначение"), max_length=255, blank=True)
    target_charge_ids = models.JSONField(_("начисления к оплате"), default=list, blank=True)

    failure_reason = models.CharField(_("причина ошибки"), max_length=255, blank=True)
    initiated_at = models.DateTimeField(_("инициирован"), auto_now_add=True)
    completed_at = models.DateTimeField(_("завершён"), null=True, blank=True)
    raw_payload = models.JSONField(_("данные банка"), default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Платёж")
        verbose_name_plural = _("Платежи")
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["provider", "provider_payment_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.amount} {self.currency} ({self.get_status_display()})"

    @property
    def is_final(self) -> bool:
        return self.status in {
            PaymentStatus.SUCCEEDED,
            PaymentStatus.FAILED,
            PaymentStatus.CANCELLED,
            PaymentStatus.REFUNDED,
        }


class PaymentAllocation(BaseModel):
    """Распределение суммы платежа по конкретным начислениям (ТЗ, п. 16)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(
        Payment, on_delete=models.CASCADE, related_name="allocations", verbose_name=_("платёж")
    )
    charge = models.ForeignKey(
        "billing.Charge",
        on_delete=models.PROTECT,
        related_name="allocations",
        verbose_name=_("начисление"),
    )
    amount = models.DecimalField(_("сумма"), max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Распределение платежа")
        verbose_name_plural = _("Распределение платежей")
        constraints = [
            models.UniqueConstraint(fields=["payment", "charge"], name="uniq_payment_charge"),
        ]
        ordering = ("created_at",)

    def __str__(self) -> str:
        return f"{self.payment_id} → {self.charge_id}: {self.amount}"


class Receipt(BaseModel):
    """Электронная квитанция, связанная с успешным платежом (ТЗ, п. 14)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.OneToOneField(
        Payment, on_delete=models.CASCADE, related_name="receipt", verbose_name=_("платёж")
    )
    number = models.CharField(_("номер квитанции"), max_length=64, unique=True)
    data = models.JSONField(_("данные квитанции"), default=dict)
    issued_at = models.DateTimeField(_("выдана"), auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Электронная квитанция")
        verbose_name_plural = _("Электронные квитанции")
        ordering = ("-issued_at",)

    def __str__(self) -> str:
        return self.number


class BankWebhookLog(BaseModel):
    """Журнал входящих уведомлений банка (идемпотентность и аудит)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(_("провайдер"), max_length=32, db_index=True)
    payload = models.JSONField(_("тело запроса"), default=dict)
    signature = models.CharField(_("подпись"), max_length=512, blank=True)
    signature_valid = models.BooleanField(_("подпись верна"), default=False)
    external_id = models.CharField(_("внешний ID"), max_length=128, blank=True, db_index=True)
    processed = models.BooleanField(_("обработан"), default=False, db_index=True)
    error = models.TextField(_("ошибка"), blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Уведомление банка")
        verbose_name_plural = _("Уведомления банка")
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.provider}: {self.external_id or '—'}"
