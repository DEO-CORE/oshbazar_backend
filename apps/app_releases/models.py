"""Поэтапный выпуск обновлений мобильного приложения (staged rollout)."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel
from apps.users.models import UserRole

semver_validator = RegexValidator(
    regex=r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$",
    message=_("Версия в формате семантического версионирования: 1.3.0"),
)


class Platform(models.TextChoices):
    ANDROID = "android", "Android"
    IOS = "ios", "iOS"


class ReleaseStatus(models.TextChoices):
    DRAFT = "draft", _("Черновик")
    ROLLING_OUT = "rolling_out", _("Раскатывается")
    PAUSED = "paused", _("Приостановлен")
    COMPLETED = "completed", _("Выпущен для всех")
    ROLLED_BACK = "rolled_back", _("Отозван")


# Статусы, в которых релиз предлагается клиентам.
ACTIVE_STATUSES = (ReleaseStatus.ROLLING_OUT, ReleaseStatus.COMPLETED)
# Статусы, в которых процент раскатки нельзя уменьшать.
LOCKED_PERCENTAGE_STATUSES = (ReleaseStatus.ROLLING_OUT, ReleaseStatus.PAUSED, ReleaseStatus.COMPLETED)


def _split_lines(raw: str) -> set[str]:
    return {line.strip() for line in (raw or "").replace(",", "\n").splitlines() if line.strip()}


class AppRelease(BaseModel):
    platform = models.CharField(_("платформа"), max_length=10, choices=Platform, db_index=True)
    version = models.CharField(_("версия"), max_length=32, validators=[semver_validator])
    build_number = models.PositiveIntegerField(
        _("номер сборки"), help_text=_("versionCode (Android) / CFBundleVersion (iOS). Растёт с каждой сборкой.")
    )
    release_notes = models.TextField(_("что нового (рус.)"), blank=True)
    release_notes_ky = models.TextField(_("что нового (кырг.)"), blank=True)
    store_url = models.URLField(_("ссылка на магазин"), max_length=500)

    rollout_percentage = models.PositiveSmallIntegerField(
        _("процент раскатки"), default=0, validators=[MaxValueValidator(100)]
    )
    status = models.CharField(
        _("статус"), max_length=16, choices=ReleaseStatus, default=ReleaseStatus.DRAFT, db_index=True
    )
    is_mandatory = models.BooleanField(
        _("обязательное обновление"),
        default=False,
        help_text=_("Пользователи, попавшие в раскатку, не смогут пропустить обновление."),
    )
    min_supported_build = models.PositiveIntegerField(
        _("минимальная поддерживаемая сборка"),
        null=True,
        blank=True,
        help_text=_(
            "Сборки ниже этой обязаны обновиться немедленно — независимо от процента раскатки "
            "(для критических исправлений)."
        ),
    )

    # --- Таргетинг ---------------------------------------------------------
    allowlisted_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="allowlisted_app_releases",
        verbose_name=_("тестировщики (пользователи)"),
        help_text=_("Всегда получают релиз, независимо от процента. Поиск по телефону, ИНН, ФИО."),
    )
    allowlisted_device_ids = models.TextField(
        _("тестировщики (ID устройств)"),
        blank=True,
        help_text=_("По одному device_id в строке — для тестов до входа в приложение."),
    )
    target_roles = models.JSONField(
        _("только для ролей"),
        default=list,
        blank=True,
        help_text=_("Пусто — для всех. Например, сначала только контролёрам."),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_app_releases",
        verbose_name=_("создал"),
    )
    started_at = models.DateTimeField(_("раскатка начата"), null=True, blank=True)
    completed_at = models.DateTimeField(_("выпущен для всех"), null=True, blank=True)
    rolled_back_at = models.DateTimeField(_("отозван"), null=True, blank=True)

    class Meta:
        verbose_name = _("Релиз приложения")
        verbose_name_plural = _("Релизы приложения")
        ordering = ("platform", "-build_number")
        constraints = [
            models.UniqueConstraint(fields=["platform", "build_number"], name="uniq_app_release_platform_build"),
            models.CheckConstraint(
                condition=models.Q(rollout_percentage__lte=100), name="app_release_percentage_lte_100"
            ),
        ]
        indexes = [models.Index(fields=["platform", "status", "build_number"])]

    def __str__(self) -> str:
        return f"{self.get_platform_display()} {self.version} ({self.build_number})"

    # --- Таргетинг ---------------------------------------------------------
    @property
    def effective_percentage(self) -> int:
        return 100 if self.status == ReleaseStatus.COMPLETED else self.rollout_percentage

    @property
    def device_allowlist(self) -> set[str]:
        return _split_lines(self.allowlisted_device_ids)

    def clean(self):
        errors = {}
        if self.min_supported_build and self.build_number and self.min_supported_build > self.build_number:
            errors["min_supported_build"] = _("Не может быть больше номера сборки этого релиза.")
        invalid_roles = set(self.target_roles or []) - set(UserRole.values)
        if invalid_roles:
            errors["target_roles"] = _("Неизвестные роли: %(roles)s") % {"roles": ", ".join(sorted(invalid_roles))}
        if self.platform and self.build_number:
            others = AppRelease.objects.filter(platform=self.platform).exclude(pk=self.pk)
            if self._state.adding:
                latest = others.order_by("-build_number").values_list("build_number", flat=True).first()
                if latest is not None and self.build_number <= latest:
                    errors["build_number"] = _(
                        "Номер сборки должен быть больше предыдущего (%(latest)s) для этой платформы."
                    ) % {"latest": latest}
            elif others.filter(build_number=self.build_number).exists():
                errors["build_number"] = _("Сборка с таким номером для этой платформы уже есть.")
        if not self._state.adding and self.pk:
            previous = AppRelease.objects.filter(pk=self.pk).values("rollout_percentage", "status").first()
            if (
                previous
                and previous["status"] in LOCKED_PERCENTAGE_STATUSES
                and self.status != ReleaseStatus.ROLLED_BACK
                and self.rollout_percentage < previous["rollout_percentage"]
            ):
                errors["rollout_percentage"] = _(
                    "Во время раскатки процент нельзя уменьшать — только отозвать релиз."
                )
        if errors:
            raise ValidationError(errors)


class AppReleaseEvent(BaseModel):
    """История действий с релизом — лента в админке и журнал."""

    release = models.ForeignKey(AppRelease, on_delete=models.CASCADE, related_name="events", verbose_name=_("релиз"))
    action = models.CharField(_("действие"), max_length=32)
    from_status = models.CharField(_("был статус"), max_length=16, blank=True)
    to_status = models.CharField(_("стал статус"), max_length=16, blank=True)
    from_percentage = models.PositiveSmallIntegerField(_("был %"), null=True, blank=True)
    to_percentage = models.PositiveSmallIntegerField(_("стал %"), null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("кто")
    )

    class Meta:
        verbose_name = _("Событие релиза")
        verbose_name_plural = _("События релизов")
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.release} — {self.action}"


class AppVersionCheckIn(BaseModel):
    """Последняя проверка версии с устройства (одна строка на устройство/пользователя)."""

    platform = models.CharField(_("платформа"), max_length=10, choices=Platform)
    rollout_key_hash = models.CharField(_("хеш ключа раскатки"), max_length=64)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="app_check_ins",
        verbose_name=_("пользователь"),
    )
    build_number = models.PositiveIntegerField(_("сборка"))
    version = models.CharField(_("версия"), max_length=32, blank=True)
    offered_build = models.PositiveIntegerField(_("предложена сборка"), null=True, blank=True)
    check_count = models.PositiveIntegerField(_("проверок"), default=1)
    last_seen_at = models.DateTimeField(_("последний визит"), db_index=True)

    class Meta:
        verbose_name = _("Проверка версии")
        verbose_name_plural = _("Проверки версии (устройства)")
        ordering = ("-last_seen_at",)
        constraints = [
            models.UniqueConstraint(fields=["platform", "rollout_key_hash"], name="uniq_app_checkin_platform_key"),
        ]
        indexes = [models.Index(fields=["platform", "last_seen_at", "build_number"])]

    def __str__(self) -> str:
        return f"{self.get_platform_display()} {self.version or ''} ({self.build_number})"


# ===========================================================================
# Фича-флаги: «изменил что-то в приложении — получают только N% пользователей»
# ===========================================================================
flag_key_validator = RegexValidator(
    regex=r"^[a-z][a-z0-9_]{1,63}$",
    message=_("Ключ: латиница в нижнем регистре, цифры и «_», начинается с буквы. Например: new_payment_screen"),
)


class FlagPlatform(models.TextChoices):
    ALL = "all", _("Все платформы")
    ANDROID = "android", "Android"
    IOS = "ios", "iOS"


class FeatureFlag(BaseModel):
    key = models.CharField(
        _("ключ"),
        max_length=64,
        unique=True,
        validators=[flag_key_validator],
        help_text=_("Используется в коде приложения. После выпуска не меняйте."),
    )
    name = models.CharField(_("название"), max_length=150)
    description = models.TextField(_("описание"), blank=True)

    is_enabled = models.BooleanField(
        _("включён"),
        default=False,
        help_text=_("Аварийный выключатель: выключенный флаг ложен для всех, включая тестировщиков."),
    )
    rollout_percentage = models.PositiveSmallIntegerField(
        _("процент пользователей"), default=0, validators=[MaxValueValidator(100)]
    )
    platform = models.CharField(_("платформа"), max_length=10, choices=FlagPlatform, default=FlagPlatform.ALL)
    min_build = models.PositiveIntegerField(_("от сборки"), null=True, blank=True)
    max_build = models.PositiveIntegerField(_("до сборки (включительно)"), null=True, blank=True)
    target_roles = models.JSONField(
        _("только для ролей"), default=list, blank=True, help_text=_("Пусто — для всех.")
    )
    allowlisted_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="allowlisted_feature_flags",
        verbose_name=_("тестировщики (пользователи)"),
        help_text=_("Всегда получают флаг, пока он включён."),
    )
    allowlisted_device_ids = models.TextField(
        _("тестировщики (ID устройств)"), blank=True, help_text=_("По одному device_id в строке.")
    )
    payload = models.JSONField(
        _("payload (удалённая конфигурация)"),
        null=True,
        blank=True,
        help_text=_('JSON, который получат пользователи с включённым флагом. Например: {"variant": "b"}'),
    )
    is_archived = models.BooleanField(
        _("в архиве"), default=False, help_text=_("Архивный флаг больше не отдаётся приложению.")
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_feature_flags",
        verbose_name=_("создал"),
    )
    enabled_at = models.DateTimeField(_("включён"), null=True, blank=True)
    disabled_at = models.DateTimeField(_("выключен"), null=True, blank=True)

    class Meta:
        verbose_name = _("Фича-флаг")
        verbose_name_plural = _("Фича-флаги")
        ordering = ("is_archived", "key")
        constraints = [
            models.CheckConstraint(condition=models.Q(rollout_percentage__lte=100), name="feature_flag_percentage_lte_100"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.key})"

    @property
    def device_allowlist(self) -> set[str]:
        return _split_lines(self.allowlisted_device_ids)

    @property
    def state(self) -> str:
        if self.is_archived:
            return "archived"
        if not self.is_enabled:
            return "off"
        if self.rollout_percentage >= 100:
            return "full"
        return "partial"

    def get_state_display(self) -> str:
        return {
            "archived": _("В архиве"),
            "off": _("Выключен"),
            "full": _("Включён для всех"),
            "partial": _("Включён для %(pct)s%%") % {"pct": self.rollout_percentage},
        }[self.state]

    def clean(self):
        errors = {}
        if self.min_build and self.max_build and self.min_build > self.max_build:
            errors["max_build"] = _("«До сборки» должно быть не меньше «от сборки».")
        if self.payload is not None and not isinstance(self.payload, dict):
            errors["payload"] = _('Payload должен быть JSON-объектом, например {"variant": "b"}.')
        invalid_roles = set(self.target_roles or []) - set(UserRole.values)
        if invalid_roles:
            errors["target_roles"] = _("Неизвестные роли: %(roles)s") % {"roles": ", ".join(sorted(invalid_roles))}
        if errors:
            raise ValidationError(errors)


class FeatureFlagEvent(BaseModel):
    flag = models.ForeignKey(FeatureFlag, on_delete=models.CASCADE, related_name="events", verbose_name=_("флаг"))
    action = models.CharField(_("действие"), max_length=32)
    summary = models.CharField(_("изменения"), max_length=255, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("кто")
    )

    class Meta:
        verbose_name = _("Событие фича-флага")
        verbose_name_plural = _("События фича-флагов")
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.flag.key} — {self.action}"
