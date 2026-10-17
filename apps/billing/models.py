"""Расчётные периоды, тарифы, начисления и журнал операций (ledger)."""
import uuid
from datetime import date, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel

MONTHS_RU = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
    7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}

class BillingPeriod(BaseModel):
    """Расчётный период (календарный месяц)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    year = models.PositiveIntegerField(_("год"), db_index=True)
    month = models.PositiveSmallIntegerField(_("месяц"), db_index=True)
    starts_on = models.DateField(_("начало периода"))
    ends_on = models.DateField(_("конец периода"))
    is_open = models.BooleanField(_("открыт для начислений"), default=True, db_index=True)
    closed_at = models.DateTimeField(_("закрыт"), null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Расчётный период")
        verbose_name_plural = _("Расчётные периоды")
        ordering = ("-year", "-month")
        constraints = [
            models.UniqueConstraint(fields=["year", "month"], name="uniq_billing_period"),
        ]

    def __str__(self) -> str:
        return self.label

    @property
    def label(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def title(self) -> str:
        return f"{MONTHS_RU.get(self.month, self.month)} {self.year}"

    @property
    def due_date(self) -> date:
        """Дата, до которой необходимо выполнить оплату."""
        from django.conf import settings as conf

        return self.ends_on + timedelta(days=getattr(conf, "PAYMENT_DUE_DAYS", 10))

    def close(self, save: bool = True) -> "BillingPeriod":
        self.is_open = False
        self.closed_at = timezone.now()
        if save:
            self.save(update_fields=["is_open", "closed_at", "updated_at"])
        return self


def period_for_date(value: date) -> BillingPeriod:
    """Возвращает (создавая при необходимости) период для даты."""
    period, _ = BillingPeriod.objects.get_or_create(
        year=value.year,
        month=value.month,
        defaults={
            "starts_on": value.replace(day=1),
            "ends_on": _month_end(value.year, value.month),
        },
    )
    return period


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


class Tariff(BaseModel):
    """Тариф на электроэнергию.

    Исторические начисления хранят снимок цены в `Charge.tariff_price`,
    поэтому изменение тарифа не меняет уже сформированные начисления (ТЗ, п. 10).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(_("название"), max_length=120)
    price = models.DecimalField(
        _("цена за кВт⋅ч"), max_digits=10, decimal_places=2, help_text=_("сом за кВт⋅ч")
    )
    currency = models.CharField(_("валюта"), max_length=8, default="KGS")
    valid_from = models.DateField(_("действует с"), db_index=True)
    valid_to = models.DateField(_("действует по"), null=True, blank=True, db_index=True)
    is_active = models.BooleanField(_("активен"), default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_tariffs",
        verbose_name=_("кто установил"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Тариф")
        verbose_name_plural = _("Тарифы")
        ordering = ("-valid_from", "-created_at")

    def __str__(self) -> str:
        return f"{self.name}: {self.price} {self.currency}"

    def clean(self):
        if self.price is not None and self.price < 0:
            raise DjangoValidationError({"price": _("Тариф не может быть отрицательным.")})
        if self.valid_to and self.valid_from and self.valid_to < self.valid_from:
            raise DjangoValidationError({"valid_to": _("Дата окончания раньше даты начала.")})

    def covers(self, value: date) -> bool:
        if value < self.valid_from:
            return False
        if self.valid_to and value > self.valid_to:
            return False
        return True


class ChargeStatus(models.TextChoices):
    UNPAID = "unpaid", _("не оплачено")
    PARTIALLY_PAID = "partial", _("частично оплачено")
    PAID = "paid", _("оплачено")
    VOID = "void", _("аннулировано")


class Charge(BaseModel):
    """Начисление за электроэнергию по точке за расчётный период."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    period = models.ForeignKey(
        BillingPeriod, on_delete=models.PROTECT, related_name="charges", verbose_name=_("расчётный период")
    )
    trade_point = models.ForeignKey(
        "trade_points.TradePoint",
        on_delete=models.PROTECT,
        related_name="charges",
        verbose_name=_("торговая точка"),
    )
    meter = models.ForeignKey(
        "trade_points.Meter",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="charges",
        verbose_name=_("электросчётчик"),
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="charges",
        verbose_name=_("плательщик"),
        null=True,
        blank=True,
    )

    # Snapshot of the reading (uuid ref avoids a circular migration dependency).
    source_reading_id = models.UUIDField(_("ID показания"), null=True, blank=True, db_index=True)
    previous_value = models.DecimalField(_("предыдущее показание"), max_digits=12, decimal_places=2, default=0)
    current_value = models.DecimalField(_("текущее показание"), max_digits=12, decimal_places=2, default=0)
    consumption = models.DecimalField(_("потребление, кВт⋅ч"), max_digits=12, decimal_places=2, default=0)

    tariff = models.ForeignKey(
        Tariff, on_delete=models.SET_NULL, null=True, blank=True, related_name="charges", verbose_name=_("тариф")
    )
    tariff_price = models.DecimalField(
        _("тариф периода (снимок)"), max_digits=10, decimal_places=2, default=0
    )
    amount = models.DecimalField(_("сумма начисления"), max_digits=12, decimal_places=2, default=0)
    paid_amount = models.DecimalField(
        _("оплачено"), max_digits=12, decimal_places=2, default=0, db_index=True
    )
    credit_applied = models.DecimalField(
        _("оплачено авансом"), max_digits=12, decimal_places=2, default=0
    )

    status = models.CharField(
        _("статус"), max_length=16, choices=ChargeStatus.choices, default=ChargeStatus.UNPAID, db_index=True
    )
    due_date = models.DateField(_("срок оплаты"), null=True, blank=True, db_index=True)
    paid_at = models.DateTimeField(_("оплачен"), null=True, blank=True)

    is_manual = models.BooleanField(_("ручное начисление"), default=False)
    note = models.TextField(_("примечание"), blank=True)
    external_id_1c = models.CharField(_("внешний ID в 1С"), max_length=64, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Начисление")
        verbose_name_plural = _("Начисления")
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["period", "status"]),
            models.Index(fields=["trade_point", "status"]),
            models.Index(fields=["owner", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["period", "meter"],
                condition=models.Q(meter__isnull=False) & ~models.Q(status="void"),
                name="uniq_charge_period_meter",
            ),
            models.UniqueConstraint(
                fields=["period", "trade_point"],
                condition=models.Q(meter__isnull=True) & ~models.Q(status="void"),
                name="uniq_charge_period_point_manual",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.period} — {self.trade_point.code}: {self.amount}"

    @property
    def remaining(self):
        return max(self.amount - self.paid_amount, 0)

    @property
    def is_paid(self) -> bool:
        return self.paid_amount >= self.amount and self.amount > 0

    def recalculate_amount(self) -> None:
        self.amount = (self.consumption * self.tariff_price).quantize("0.01")
        self.save(update_fields=["amount", "updated_at"])

    def sync_status(self) -> None:
        """Пересчёт статуса начисления по фактически оплаченной сумме."""
        if self.status == ChargeStatus.VOID:
            return
        if self.paid_amount <= 0:
            new_status = ChargeStatus.UNPAID
        elif self.paid_amount < self.amount:
            new_status = ChargeStatus.PARTIALLY_PAID
        else:
            new_status = ChargeStatus.PAID
        fields = ["status", "updated_at"]
        if new_status == ChargeStatus.PAID and not self.paid_at:
            self.paid_at = timezone.now()
            fields.append("paid_at")
        if new_status != ChargeStatus.PAID and self.paid_at:
            self.paid_at = None
            fields.append("paid_at")
        self.status = new_status
        fields += ["paid_amount", "credit_applied"]
        self.save(update_fields=fields)


class LedgerEntry(BaseModel):
    """Неизменяемый журнал финансовых операций пользователя.

    `amount > 0` увеличивает задолженность (начисление),
    `amount < 0` уменьшает её (оплата/аванс). Баланс пользователя — сумма записей:
    положительная сумма = задолженность, отрицательная = переплата (аванс).
    """

    class Kind(models.TextChoices):
        CHARGE = "charge", _("начисление")
        PAYMENT = "payment", _("оплата")
        CREDIT = "credit", _("аванс / переплата")
        ADJUSTMENT = "adjustment", _("корректировка")
        REFUND = "refund", _("возврат")

    class Source(models.TextChoices):
        MONEY = "money", _("банковский платёж")
        CREDIT = "credit", _("списание аванса")
        MANUAL = "manual", _("ручная операция")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
        verbose_name=_("пользователь"),
        null=True,
        blank=True,
    )
    trade_point = models.ForeignKey(
        "trade_points.TradePoint",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entries",
        verbose_name=_("торговая точка"),
    )
    charge = models.ForeignKey(
        Charge, on_delete=models.PROTECT, null=True, blank=True, related_name="ledger_entries", verbose_name=_("начисление")
    )
    payment_ref = models.UUIDField(_("ID платежа"), null=True, blank=True, db_index=True)

    kind = models.CharField(_("тип операции"), max_length=16, choices=Kind.choices, db_index=True)
    source = models.CharField(_("источник"), max_length=16, choices=Source.choices, default=Source.MONEY)
    amount = models.DecimalField(_("сумма со знаком"), max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(
        _("баланс после операции"), max_digits=12, decimal_places=2, default=0
    )
    note = models.CharField(_("примечание"), max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Запись журнала расчётов")
        verbose_name_plural = _("Журнал расчётов")
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind}: {self.amount} ({self.user})"
