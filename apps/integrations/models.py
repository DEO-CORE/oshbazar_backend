"""Сущности интеграции с 1С (ТЗ, п. 6).

Перечень данных, направление и протокол синхронизации уточняются заказчиком,
поэтому модель хранит произвольные payload'ы и журнал запусков.
"""
import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel


class SyncEntity(models.TextChoices):
    TENANT = "tenant", _("Арендатор")
    TRADE_POINT = "trade_point", _("Торговая точка")
    METER = "meter", _("Электросчётчик")
    READING = "reading", _("Показание")
    CHARGE = "charge", _("Начисление")
    PAYMENT = "payment", _("Платёж")
    TARIFF = "tariff", _("Тариф")


class SyncDirection(models.TextChoices):
    IMPORT = "import", _("1С → приложение")
    EXPORT = "export", _("приложение → 1С")


class SyncStatus(models.TextChoices):
    RUNNING = "running", _("выполняется")
    SUCCESS = "success", _("успешно")
    PARTIAL = "partial", _("частично")
    FAILED = "failed", _("ошибка")


class SyncRun(BaseModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entity = models.CharField(_("сущность"), max_length=32, choices=SyncEntity.choices, db_index=True)
    direction = models.CharField(
        _("направление"), max_length=16, choices=SyncDirection.choices, default=SyncDirection.IMPORT
    )
    status = models.CharField(_("статус"), max_length=16, choices=SyncStatus.choices,
                              default=SyncStatus.RUNNING, db_index=True)
    started_at = models.DateTimeField(_("начало"), auto_now_add=True)
    finished_at = models.DateTimeField(_("окончание"), null=True, blank=True)
    stats = models.JSONField(_("статистика"), default=dict, blank=True)
    error = models.TextField(_("ошибка"), blank=True)

    class Meta:
        verbose_name = _("Запуск синхронизации")
        verbose_name_plural = _("Запуски синхронизации")
        ordering = ("-started_at",)

    def __str__(self) -> str:
        return f"{self.get_entity_display()} [{self.direction}] — {self.get_status_display()}"


class ExternalObject(BaseModel):
    """Связь внешнего идентификатора 1С с внутренним объектом."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entity = models.CharField(_("сущность"), max_length=32, choices=SyncEntity.choices, db_index=True)
    external_id = models.CharField(_("ID в 1С"), max_length=128, db_index=True)
    internal_id = models.UUIDField(_("ID в приложении"), null=True, blank=True, db_index=True)
    payload = models.JSONField(_("данные"), default=dict, blank=True)
    checksum = models.CharField(_("контрольная сумма"), max_length=64, blank=True)
    synced_at = models.DateTimeField(_("синхронизировано"), auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Объект 1С")
        verbose_name_plural = _("Объекты 1С")
        constraints = [
            models.UniqueConstraint(fields=["entity", "external_id"], name="uniq_external_object"),
        ]
        ordering = ("-synced_at",)

    def __str__(self) -> str:
        return f"{self.get_entity_display()}: {self.external_id}"
