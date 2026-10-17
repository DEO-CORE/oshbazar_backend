"""Custom user model with PIN-code authentication and role based access."""
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel

PIN_MAX_ATTEMPTS = 5
PIN_LOCK_MINUTES = 15

pin_validator = RegexValidator(
    regex=r"^\d{4,6}$",
    message=_("ПИН-код должен состоять из 4–6 цифр."),
)


class UserRole(models.TextChoices):
    TENANT = "tenant", _("Арендатор / владелец торговой точки")
    CONTROLLER = "controller", _("Контролёр")
    ADMIN = "admin", _("Администратор")
    SUPER_ADMIN = "super_admin", _("Супер-администратор")


class Language(models.TextChoices):
    RU = "ru", "Русский"
    KY = "ky", "Кыргызча"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _normalize_phone(self, phone: str) -> str:
        return re.sub(r"[^\d+]", "", phone or "")

    def create_user(self, phone: str, pin: str | None = None, **extra):
        if not phone:
            raise ValueError("User must have a phone number")
        user = self.model(phone=self._normalize_phone(phone), **extra)
        if pin:
            user.set_pin(pin)
        user.save(using=self._db)
        return user

    def create_superuser(self, phone: str, pin: str | None = None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        extra.setdefault("role", UserRole.SUPER_ADMIN)
        if extra.get("is_staff") is not True or extra.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_staff=True and is_superuser=True.")
        return self.create_user(phone, pin, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """Арендаторы и сотрудники Ошского базара.

    Идентификатор для входа — номер телефона (или логин), секрет — ПИН-код.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    phone = models.CharField(_("номер телефона"), max_length=32, unique=True, db_index=True)
    username = models.CharField(
        _("логин"), max_length=150, unique=True, blank=True, null=True, db_index=True
    )
    first_name = models.CharField(_("имя"), max_length=150, blank=True)
    last_name = models.CharField(_("фамилия"), max_length=150, blank=True)
    email = models.EmailField(_("e-mail"), blank=True)

    role = models.CharField(
        _("роль"),
        max_length=20,
        choices=UserRole,
        default=UserRole.TENANT,
        db_index=True,
    )
    language = models.CharField(
        _("язык интерфейса"), max_length=2, choices=Language, default=Language.RU
    )

    # --- PIN storage -------------------------------------------------------
    pin = models.CharField(_("хеш ПИН-кода"), max_length=128, blank=True)
    pin_set_at = models.DateTimeField(_("ПИН установлен"), null=True, blank=True)
    failed_pin_attempts = models.PositiveSmallIntegerField(_("неудачных попыток"), default=0)
    pin_locked_until = models.DateTimeField(_("ПИН заблокирован до"), null=True, blank=True)
    is_blocked = models.BooleanField(_("заблокирован администратором"), default=False)

    is_active = models.BooleanField(_("активен"), default=True)
    is_staff = models.BooleanField(_("персонал"), default=False)
    created_at = models.DateTimeField(_("создан"), auto_now_add=True)
    updated_at = models.DateTimeField(_("обновлён"), auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        verbose_name = _("Пользователь")
        verbose_name_plural = _("Пользователи")
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["role", "is_active"]),
        ]

    def __str__(self) -> str:
        return self.full_name or self.phone

    @property
    def full_name(self) -> str:
        return " ".join(part for part in [self.last_name, self.first_name] if part).strip()

    # --- PIN helpers -------------------------------------------------------
    def set_pin(self, pin: str) -> None:
        pin_validator(pin)
        self.pin = make_password(pin)
        self.pin_set_at = timezone.now()
        self.failed_pin_attempts = 0
        self.pin_locked_until = None

    def has_usable_pin(self) -> bool:
        return bool(self.pin) and not self.pin.startswith("!")

    def check_pin(self, pin: str) -> bool:
        if not self.has_usable_pin():
            return False
        return check_password(pin, self.pin)

    @property
    def is_pin_locked(self) -> bool:
        return bool(self.pin_locked_until and self.pin_locked_until > timezone.now())

    def register_failed_pin(self) -> None:
        self.failed_pin_attempts += 1
        if self.failed_pin_attempts >= PIN_MAX_ATTEMPTS:
            self.pin_locked_until = timezone.now() + timedelta(minutes=PIN_LOCK_MINUTES)
            self.failed_pin_attempts = 0
        self.save(update_fields=["failed_pin_attempts", "pin_locked_until", "updated_at"])

    def reset_pin_attempts(self) -> None:
        if self.failed_pin_attempts or self.pin_locked_until:
            self.failed_pin_attempts = 0
            self.pin_locked_until = None
            self.save(update_fields=["failed_pin_attempts", "pin_locked_until", "updated_at"])

    @property
    def can_submit_readings(self) -> bool:
        return self.role in {UserRole.TENANT, UserRole.CONTROLLER, UserRole.ADMIN, UserRole.SUPER_ADMIN}

    @property
    def is_staff_role(self) -> bool:
        return self.role in {UserRole.CONTROLLER, UserRole.ADMIN, UserRole.SUPER_ADMIN} or self.is_superuser


class PinRecoveryRequest(BaseModel):
    """Запрос на восстановление ПИН-кода.

    Механизм выдачи/восстановления ПИН-кода утверждается заказчиком (ТЗ, п. 4, п. 25.30–25.31).
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("на рассмотрении")
        APPROVED = "approved", _("одобрен")
        REJECTED = "rejected", _("отклонён")

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="pin_recovery_requests", verbose_name=_("пользователь")
    )
    comment = models.TextField(_("комментарий пользователя"), blank=True)
    status = models.CharField(_("статус"), max_length=16, choices=Status, default=Status.PENDING)
    decided_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pin_recovery_decisions",
        verbose_name=_("рассмотрел"),
    )
    decided_at = models.DateTimeField(_("рассмотрен"), null=True, blank=True)
    decision_comment = models.TextField(_("комментарий администратора"), blank=True)

    class Meta:
        verbose_name = _("Запрос на восстановление ПИН-кода")
        verbose_name_plural = _("Запросы на восстановление ПИН-кода")
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.user} — {self.status}"
