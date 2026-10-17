"""Торговые точки, доступы пользователей и электросчётчики."""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel


class TradePoint(BaseModel):
    """Торговая точка Ошского базара (до ~5 000 записей)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    code = models.CharField(
        _("код / номер точки"),
        max_length=32,
        unique=True,
        db_index=True,
        help_text=_("Уникальный код, кодируется в QR-код точки."),
    )
    name = models.CharField(_("название"), max_length=255)
    block = models.CharField(_("корпус / ряд"), max_length=64, blank=True)
    place_number = models.CharField(_("номер места"), max_length=32, blank=True)
    address = models.CharField(_("адрес"), max_length=255, blank=True)
    area_m2 = models.DecimalField(
        _("площадь, м²"), max_digits=8, decimal_places=2, null=True, blank=True
    )

    qr_payload = models.CharField(
        _("данные QR-кода"),
        max_length=255,
        blank=True,
        help_text=_("Если пусто — формируется из кода точки."),
    )
    external_id_1c = models.CharField(
        _("внешний ID в 1С"), max_length=64, blank=True, db_index=True
    )

    is_active = models.BooleanField(_("активна"), default=True, db_index=True)
    assigned_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="TradePointAccess",
        through_fields=("trade_point", "user"),
        related_name="trade_points",
        verbose_name=_("пользователи"),
    )
    created_at = models.DateTimeField(_("создана"), auto_now_add=True)
    updated_at = models.DateTimeField(_("обновлена"), auto_now=True)

    class Meta:
        verbose_name = _("Торговая точка")
        verbose_name_plural = _("Торговые точки")
        ordering = ("block", "place_number", "code")
        indexes = [
            models.Index(fields=["is_active", "block"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} — {self.name}"

    @property
    def qr_data(self) -> str:
        return self.qr_payload or f"OSHBAZAR:TP:{self.code}"


class AccessRole(models.TextChoices):
    OWNER = "owner", _("Владелец")
    TENANT = "tenant", _("Арендатор")
    EMPLOYEE = "employee", _("Сотрудник точки")


class TradePointAccess(BaseModel):
    """Связь «пользователь ↔ торговая точка» (у одного — несколько точек)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trade_point_accesses",
        verbose_name=_("пользователь"),
    )
    trade_point = models.ForeignKey(
        TradePoint,
        on_delete=models.CASCADE,
        related_name="user_accesses",
        verbose_name=_("торговая точка"),
    )
    access_role = models.CharField(
        _("роль на точке"), max_length=16, choices=AccessRole.choices, default=AccessRole.TENANT
    )
    is_active = models.BooleanField(_("активен"), default=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_accesses",
        verbose_name=_("кто выдал доступ"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Доступ к торговой точке")
        verbose_name_plural = _("Доступы к торговым точкам")
        constraints = [
            models.UniqueConstraint(fields=["user", "trade_point"], name="uniq_user_trade_point"),
        ]
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.user} → {self.trade_point}"


class Meter(BaseModel):
    """Электросчётчик торговой точки.

    Количество счётчиков на одну точку уточняется заказчиком (ТЗ, п. 25.7),
    поэтому модель поддерживает несколько счётчиков с признаком основного.
    """

    trade_point = models.ForeignKey(
        TradePoint,
        on_delete=models.CASCADE,
        related_name="meters",
        verbose_name=_("торговая точка"),
    )
    serial_number = models.CharField(
        _("заводской номер"), max_length=64, unique=True, db_index=True
    )
    model_name = models.CharField(_("модель"), max_length=64, blank=True)
    initial_reading = models.DecimalField(
        _("начальное показание"),
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text=_("Показание на момент установки / ввода в эксплуатацию."),
    )
    installed_at = models.DateField(_("дата установки"), null=True, blank=True)
    deactivated_at = models.DateField(_("дата демонтажа"), null=True, blank=True)
    is_primary = models.BooleanField(_("основной счётчик"), default=True)
    is_active = models.BooleanField(_("активен"), default=True, db_index=True)
    external_id_1c = models.CharField(_("внешний ID в 1С"), max_length=64, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Электросчётчик")
        verbose_name_plural = _("Электросчётчики")
        ordering = ("-is_primary", "serial_number")
        constraints = [
            models.UniqueConstraint(
                fields=["trade_point"],
                condition=models.Q(is_primary=True, is_active=True),
                name="uniq_primary_meter_per_point",
            )
        ]

    def __str__(self) -> str:
        return f"{self.serial_number} ({self.trade_point.code})"

    @property
    def display_name(self) -> str:
        return f"{self.serial_number} {self.model_name}".strip()

    def clean(self):
        if self.is_primary and self.is_active:
            exists = (
                Meter.objects.filter(
                    trade_point=self.trade_point, is_primary=True, is_active=True
                )
                .exclude(pk=self.pk)
                .exists()
            )
            if exists:
                raise DjangoValidationError(
                    {"is_primary": _("У точки уже есть основной активный счётчик.")}
                )

    @property
    def qr_data(self) -> str:
        return f"OSHBAZAR:METER:{self.serial_number}"
