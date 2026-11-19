"""Вычисление фича-флагов для клиента и управление ими."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.core.audit import log_action

from .models import AppVersionCheckIn, FeatureFlag, FeatureFlagEvent, FlagPlatform
from .services import ACTIVE_DEVICE_WINDOW_DAYS, RolloutSubject

FLAG_PRESETS = (1, 5, 10, 25, 50, 100)


def flag_bucket(flag_key: str, rollout_key: str) -> int:
    """Бакет 0..99: sha256(f"{flag.key}:{rollout_key}") % 100 — стабилен для пары флаг/ключ."""
    return int(hashlib.sha256(f"{flag_key}:{rollout_key}".encode()).hexdigest(), 16) % 100


def ttl_seconds() -> int:
    return int(getattr(settings, "FEATURE_FLAGS_TTL_SECONDS", 300))


@dataclass(frozen=True)
class FlagResult:
    value: bool
    reason: str
    bucket: int | None = None


REASONS = {
    "off": gettext_lazy("выключен (аварийный выключатель)"),
    "platform": gettext_lazy("другая платформа"),
    "build": gettext_lazy("сборка вне диапазона"),
    "allowlist": gettext_lazy("в списке тестировщиков"),
    "role": gettext_lazy("роль не входит в таргетинг"),
    "no_key": gettext_lazy("нет ключа (ни JWT, ни device_id)"),
    "in": gettext_lazy("попал в процент"),
    "out": gettext_lazy("не попал в процент"),
}


def evaluate_flag(
    flag: FeatureFlag,
    subject: RolloutSubject,
    *,
    platform: str,
    build: int | None,
    allowlisted_user_ids: set | None = None,
) -> FlagResult:
    key = subject.rollout_key
    bucket = flag_bucket(flag.key, key) if key is not None else None
    if not flag.is_enabled:
        return FlagResult(False, "off", bucket)
    if flag.platform != FlagPlatform.ALL and flag.platform != platform:
        return FlagResult(False, "platform", bucket)
    if build is not None and (
        (flag.min_build is not None and build < flag.min_build)
        or (flag.max_build is not None and build > flag.max_build)
    ):
        return FlagResult(False, "build", bucket)
    if allowlisted_user_ids is None:
        allowlisted_user_ids = set(flag.allowlisted_users.values_list("pk", flat=True))
    user_pk = getattr(subject.user, "pk", None) if subject.user is not None else None
    if (user_pk is not None and user_pk in allowlisted_user_ids) or (
        subject.device_id and subject.device_id in flag.device_allowlist
    ):
        return FlagResult(True, "allowlist", bucket)
    if flag.target_roles and subject.role not in flag.target_roles:
        return FlagResult(False, "role", bucket)
    if flag.rollout_percentage >= 100:
        return FlagResult(True, "in", bucket)
    if bucket is None:
        return FlagResult(False, "no_key", None)
    return FlagResult(bucket < flag.rollout_percentage, "in" if bucket < flag.rollout_percentage else "out", bucket)


def active_flags():
    return FeatureFlag.objects.filter(is_archived=False).prefetch_related("allowlisted_users").order_by("key")


def evaluate_all(subject: RolloutSubject, *, platform: str, build: int | None) -> list[tuple[FeatureFlag, FlagResult]]:
    return [
        (
            flag,
            evaluate_flag(
                flag,
                subject,
                platform=platform,
                build=build,
                allowlisted_user_ids={u.pk for u in flag.allowlisted_users.all()},
            ),
        )
        for flag in active_flags()
    ]


def features_payload(subject: RolloutSubject, *, platform: str, build: int) -> dict:
    flags, payloads = {}, {}
    for flag, result in evaluate_all(subject, platform=platform, build=build):
        flags[flag.key] = result.value
        if result.value and flag.payload is not None:
            payloads[flag.key] = flag.payload
    return {"flags": flags, "payloads": payloads, "ttl_seconds": ttl_seconds()}


# ---------------------------------------------------------------------------
# Изменения из админки
# ---------------------------------------------------------------------------
class FlagError(ValidationError):
    pass


ACTION_LABELS = {
    "create": gettext_lazy("Создан"),
    "update": gettext_lazy("Изменён"),
    "enable": gettext_lazy("Включён"),
    "disable": gettext_lazy("Выключен"),
    "set_percentage": gettext_lazy("Изменён процент"),
}


def log_flag_event(flag: FeatureFlag, action: str, summary: str = "", *, user=None, request=None):
    FeatureFlagEvent.objects.create(
        flag=flag,
        action=action,
        summary=summary[:255],
        user=user if getattr(user, "is_authenticated", False) else None,
    )
    log_action(
        user=user,
        action=f"feature_flag.{action}",
        target=flag,
        request=request,
        extra={"key": flag.key, "enabled": flag.is_enabled, "percentage": flag.rollout_percentage, "summary": summary},
    )


@transaction.atomic
def apply_flag_action(flag: FeatureFlag, action: str, *, percentage=None, user=None, request=None) -> FeatureFlag:
    flag = FeatureFlag.objects.select_for_update().get(pk=flag.pk)
    if flag.is_archived:
        raise FlagError(_("Флаг в архиве — сначала верните его из архива."))
    now = timezone.now()
    if action == "enable":
        if flag.is_enabled:
            return flag
        flag.is_enabled, flag.enabled_at = True, now
        summary = f"включён, {flag.rollout_percentage}%"
    elif action == "disable":
        if not flag.is_enabled:
            return flag
        flag.is_enabled, flag.disabled_at = False, now
        summary = "выключен для всех"
    elif action == "set_percentage":
        try:
            pct = int(percentage)
        except (TypeError, ValueError):
            raise FlagError(_("Процент должен быть целым числом от 0 до 100.")) from None
        if not 0 <= pct <= 100:
            raise FlagError(_("Процент должен быть целым числом от 0 до 100."))
        if pct == flag.rollout_percentage:
            return flag
        summary = f"{flag.rollout_percentage}% → {pct}%"
        flag.rollout_percentage = pct
    else:
        raise FlagError(_("Неизвестное действие."))
    flag.save()
    log_flag_event(flag, action, summary, user=user, request=request)
    return flag


# ---------------------------------------------------------------------------
# Оценка охвата по таблице проверок
# ---------------------------------------------------------------------------
def reach_index() -> list[dict]:
    """Один агрегирующий запрос: устройства за 30 дней по (платформа, сборка, роль).

    Строится один раз на страницу списка, дальше охват каждого флага считается в памяти.
    """
    since = timezone.now() - timedelta(days=ACTIVE_DEVICE_WINDOW_DAYS)
    return list(
        AppVersionCheckIn.objects.filter(last_seen_at__gte=since)
        .values("platform", "build_number", "user__role")
        .annotate(n=Count("id"))
    )


def _row_matches(flag: FeatureFlag, row: dict) -> bool:
    if flag.platform != FlagPlatform.ALL and row["platform"] != flag.platform:
        return False
    if flag.min_build is not None and row["build_number"] < flag.min_build:
        return False
    if flag.max_build is not None and row["build_number"] > flag.max_build:
        return False
    if flag.target_roles and row["user__role"] not in flag.target_roles:
        return False
    return True


def flag_reach(flag: FeatureFlag, index: list[dict] | None = None) -> dict:
    if index is None:
        index = reach_index()
    eligible = sum(row["n"] for row in index if _row_matches(flag, row))
    live = flag.is_enabled and not flag.is_archived
    pct = flag.rollout_percentage if live else 0
    testers = getattr(flag, "allowlist_count", None)
    if testers is None:
        testers = flag.allowlisted_users.count()
    testers += len(flag.device_allowlist)
    return {
        "eligible": eligible,
        "estimated": round(eligible * pct / 100) + (testers if live and pct < 100 else 0),
        "testers": testers,
        "pct": pct,
    }
