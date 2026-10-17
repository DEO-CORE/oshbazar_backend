"""Уведомления и устройства для push-рассылки (ТЗ, п. 17)."""
import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel


class NotificationType(models.TextChoices):
    READING_RECEIVED = "reading_received", _("Получены показания")
    READING_REJECTED = "reading_rejected", _("Показание отклонено")
    CHARGE_CREATED = "charge_created", _("Сформировано начисление")
    PAYMENT_DUE_SOON = "payment_due_soon", _("Приближается срок оплаты")
    PAYMENT_DUE_NOW = "payment_due_now", _("Наступил срок оплаты")
    DEBT_REMINDER = "debt_reminder", _("Напоминание о задолженности")
    PAYMENT_SUCCESS = "payment_success", _("Платёж получен")
    PAYMENT_FAILED = "payment_failed", _("Ошибка платежа")
    SYSTEM = "system", _("Системное уведомление")


class Notification(BaseModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
        verbose_name=_("получатель"),
    )
    type = models.CharField(
        _("тип"), max_length=32, choices=NotificationType.choices,
        default=NotificationType.SYSTEM, db_index=True,
    )
    title = models.CharField(_("заголовок"), max_length=255)
    body = models.TextField(_("текст"))
    params = models.JSONField(_("параметры"), default=dict, blank=True)

    is_read = models.BooleanField(_("прочитано"), default=False, db_index=True)
    read_at = models.DateTimeField(_("прочитано в"), null=True, blank=True)
    push_sent = models.BooleanField(_("push отправлен"), default=False)
    push_error = models.CharField(_("ошибка push"), max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Уведомление")
        verbose_name_plural = _("Уведомления")
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "is_read", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_type_display()}: {self.title}"


class DevicePlatform(models.TextChoices):
    ANDROID = "android", "Android"
    IOS = "ios", "iOS"
    WEB = "web", _("Веб")


class DeviceToken(BaseModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="device_tokens",
        verbose_name=_("пользователь"),
    )
    token = models.CharField(_("push-токен"), max_length=512, unique=True, db_index=True)
    platform = models.CharField(
        _("платформа"), max_length=16, choices=DevicePlatform.choices, default=DevicePlatform.ANDROID
    )
    locale = models.CharField(_("язык"), max_length=2, default="ru")
    is_active = models.BooleanField(_("активен"), default=True, db_index=True)
    last_seen_at = models.DateTimeField(_("активен в последний раз"), auto_now=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Устройство для push")
        verbose_name_plural = _("Устройства для push")
        ordering = ("-last_seen_at",)

    def __str__(self) -> str:
        return f"{self.get_platform_display()}: {self.token[:24]}…"
