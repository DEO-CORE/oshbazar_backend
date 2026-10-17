"""Показания электросчётчиков и фотографии-подтверждения."""
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel


class ReadingStatus(models.TextChoices):
    PENDING = "pending", _("ожидает проверки")
    APPROVED = "approved", _("принято")
    REJECTED = "rejected", _("отклонено")


class ReadingSource(models.TextChoices):
    TENANT = "tenant", _("арендатор")
    CONTROLLER = "controller", _("контролёр")
    ADMIN = "admin", _("администратор")
    IMPORT_1C = "import_1c", _("импорт из 1С")


class ReadingPhoto(BaseModel):
    """Фотография электросчётчика как подтверждение показания (ТЗ, п. 21)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.ImageField(
        _("фотография"),
        upload_to="readings/%Y/%m/",
        max_length=500,
    )
    content_type = models.CharField(_("тип файла"), max_length=64, blank=True)
    size_bytes = models.PositiveIntegerField(_("размер, байт"), default=0)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="uploaded_reading_photos",
        verbose_name=_("кто загрузил"),
    )
    trade_point = models.ForeignKey(
        "trade_points.TradePoint",
        on_delete=models.CASCADE,
        related_name="reading_photos",
        verbose_name=_("торговая точка"),
    )
    meter = models.ForeignKey(
        "trade_points.Meter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reading_photos",
        verbose_name=_("счётчик"),
    )
    original_value = models.DecimalField(
        _("показание на фотографии"), max_digits=12, decimal_places=2, null=True, blank=True
    )
    uploaded_at = models.DateTimeField(_("загружено"), auto_now_add=True)

    class Meta:
        verbose_name = _("Фотография показания")
        verbose_name_plural = _("Фотографии показаний")
        ordering = ("-uploaded_at",)

    def __str__(self) -> str:
        return f"Фото {self.trade_point.code} от {self.uploaded_at:%Y-%m-%d %H:%M}"


class MeterReading(BaseModel):
    """Переданное показание электросчётчика за расчётный период."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    meter = models.ForeignKey(
        "trade_points.Meter",
        on_delete=models.PROTECT,
        related_name="readings",
        verbose_name=_("электросчётчик"),
    )
    trade_point = models.ForeignKey(
        "trade_points.TradePoint",
        on_delete=models.PROTECT,
        related_name="readings",
        verbose_name=_("торговая точка"),
    )
    period = models.ForeignKey(
        "billing.BillingPeriod",
        on_delete=models.PROTECT,
        related_name="readings",
        verbose_name=_("расчётный период"),
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="readings",
        verbose_name=_("кто передал"),
    )

    previous_value = models.DecimalField(
        _("предыдущее показание"), max_digits=12, decimal_places=2, default=0
    )
    value = models.DecimalField(_("текущее показание"), max_digits=12, decimal_places=2)

    photo = models.ForeignKey(
        ReadingPhoto,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="readings",
        verbose_name=_("фотография счётчика"),
    )

    status = models.CharField(
        _("статус"), max_length=16, choices=ReadingStatus.choices,
        default=ReadingStatus.PENDING, db_index=True,
    )
    source = models.CharField(
        _("источник"), max_length=16, choices=ReadingSource.choices, default=ReadingSource.TENANT
    )
    rejection_reason = models.TextField(_("причина отклонения"), blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_readings",
        verbose_name=_("проверил"),
    )
    reviewed_at = models.DateTimeField(_("проверено"), null=True, blank=True)
    submitted_at = models.DateTimeField(_("передано"), auto_now_add=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Показание")
        verbose_name_plural = _("Показания")
        ordering = ("-submitted_at",)
        indexes = [
            models.Index(fields=["trade_point", "period"]),
            models.Index(fields=["meter", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["meter", "period"],
                condition=~models.Q(status="rejected"),
                name="uniq_reading_meter_period",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.trade_point.code} {self.period.label}: {self.value}"

    @property
    def consumption(self) -> Decimal:
        """Потребление = текущее показание − предыдущее (ТЗ, п. 9)."""
        return max(Decimal(self.value) - Decimal(self.previous_value), Decimal("0"))

    @property
    def is_reviewable(self) -> bool:
        return self.status == ReadingStatus.PENDING

    def clean(self):
        if self.value is not None and self.value < 0:
            raise DjangoValidationError({"value": _("Показание не может быть отрицательным.")})
        if (
            self.value is not None
            and self.previous_value is not None
            and self.value < self.previous_value
            and not getattr(settings, "READINGS_ALLOW_DECREASE", False)
        ):
            raise DjangoValidationError(
                {
                    "value": _(
                        "Текущее показание (%(value)s) меньше предыдущего (%(previous)s). "
                        "Проверьте введённые данные."
                    )
                    % {"value": self.value, "previous": self.previous_value}
                }
            )

    @property
    def display_value(self) -> str:
        return f"{self.value} кВт⋅ч"
