"""Логика поэтапной раскатки: бакетинг, выбор релиза, переходы статусов, статистика."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Max
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.core.audit import log_action

from .models import (
    ACTIVE_STATUSES,
    AppRelease,
    AppReleaseEvent,
    AppVersionCheckIn,
    ReleaseStatus,
)

ROLLOUT_PRESETS = (1, 5, 10, 25, 50, 100)
ACTIVE_DEVICE_WINDOW_DAYS = 30


# ---------------------------------------------------------------------------
# Бакетинг
# ---------------------------------------------------------------------------
def rollout_bucket(release_id, rollout_key: str) -> int:
    """Детерминированный бакет 0..99 для пары (релиз, ключ).

    Ключ и релиз фиксированы, поэтому при росте процента пользователь,
    уже попавший в раскатку, из неё не выпадает.
    """
    digest = hashlib.sha256(f"{release_id}:{rollout_key}".encode()).hexdigest()
    return int(digest, 16) % 100


def hash_key(rollout_key: str) -> str:
    return hashlib.sha256(rollout_key.encode()).hexdigest()


@dataclass(frozen=True)
class RolloutSubject:
    """Кто спрашивает: пользователь (по JWT) и/или устройство."""

    user: object | None = None
    device_id: str = ""

    @property
    def rollout_key(self) -> str | None:
        if self.user is not None and getattr(self.user, "is_authenticated", False):
            return str(self.user.pk)
        return self.device_id or None

    @property
    def role(self) -> str | None:
        return getattr(self.user, "role", None) if self.user is not None else None


def is_targeted(release: AppRelease, subject: RolloutSubject, allowlisted_user_ids: set | None = None) -> bool:
    """Попадает ли субъект в раскатку релиза (статус релиза проверяется вызывающим)."""
    if allowlisted_user_ids is None:
        allowlisted_user_ids = set(release.allowlisted_users.values_list("pk", flat=True))
    user_pk = getattr(subject.user, "pk", None) if subject.user is not None else None
    if user_pk is not None and user_pk in allowlisted_user_ids:
        return True
    if subject.device_id and subject.device_id in release.device_allowlist:
        return True
    if release.target_roles and subject.role not in release.target_roles:
        return False
    if release.status == ReleaseStatus.COMPLETED:
        return True
    key = subject.rollout_key
    if key is None:
        return False
    return rollout_bucket(release.pk, key) < release.rollout_percentage


def applies_to(release: AppRelease, subject: RolloutSubject, allowlisted_user_ids: set) -> bool:
    """Адресован ли релиз субъекту по ролям/спискам (без учёта процента)."""
    if not release.target_roles:
        return True
    user_pk = getattr(subject.user, "pk", None) if subject.user is not None else None
    if user_pk is not None and user_pk in allowlisted_user_ids:
        return True
    if subject.device_id and subject.device_id in release.device_allowlist:
        return True
    return subject.role in release.target_roles


# ---------------------------------------------------------------------------
# Выбор релиза для клиента
# ---------------------------------------------------------------------------
@dataclass
class VersionDecision:
    update_available: bool = False
    force_update: bool = False
    release: AppRelease | None = None
    min_supported_build: int | None = None

    def as_payload(self, language: str = "ru") -> dict:
        release = self.release
        if release is None:
            return {
                "update_available": False,
                "force_update": False,
                "latest_version": None,
                "latest_build": None,
                "min_supported_build": None,
                "store_url": None,
                "release_notes": None,
                "rollout_percentage": None,
            }
        notes = release.release_notes
        if language == "ky" and release.release_notes_ky:
            notes = release.release_notes_ky
        return {
            "update_available": self.update_available,
            "force_update": self.force_update,
            "latest_version": release.version,
            "latest_build": release.build_number,
            "min_supported_build": self.min_supported_build,
            "store_url": release.store_url,
            "release_notes": notes,
            "rollout_percentage": release.effective_percentage,
        }


def resolve_update(platform: str, client_build: int, subject: RolloutSubject) -> VersionDecision:
    active = list(
        AppRelease.objects.filter(platform=platform, status__in=ACTIVE_STATUSES)
        .prefetch_related("allowlisted_users")
        .order_by("-build_number")
    )
    if not active:
        return VersionDecision()

    allowlists = {r.pk: {u.pk for u in r.allowlisted_users.all()} for r in active}
    # Принудительное обновление касается только релизов, адресованных этому субъекту
    # (без ролевого таргетинга — всем): релиз «только контролёрам» не форсит арендаторов.
    applicable = [r for r in active if applies_to(r, subject, allowlists[r.pk])]
    min_required = max((r.min_supported_build or 0) for r in applicable) if applicable else 0
    min_required = min_required or None
    newer = [r for r in applicable if r.build_number > client_build]
    eligible = [r for r in newer if is_targeted(r, subject, allowlists[r.pk])]

    target = eligible[0] if eligible else None
    force_by_min = min_required is not None and client_build < min_required
    if force_by_min and (target is None or target.build_number < min_required):
        # Критическое исправление обязано дойти до всех — процент игнорируется.
        # Берём самую раннюю подходящую сборку: это и есть исправление, а не свежий эксперимент.
        target = min((r for r in newer if r.build_number >= min_required), key=lambda r: r.build_number, default=None)
    if target is None:
        return VersionDecision(min_supported_build=min_required)

    force = force_by_min or any(r.is_mandatory for r in eligible if r.build_number <= target.build_number)
    return VersionDecision(
        update_available=True, force_update=force, release=target, min_supported_build=min_required
    )


_UNSET = object()


def record_check_in(
    *, platform: str, build: int, version: str, subject: RolloutSubject, offered=_UNSET
) -> AppVersionCheckIn | None:
    """Upsert строки устройства. `offered` передаёт только проверка версии."""
    key = subject.rollout_key
    if key is None:
        return None
    prefix = "user" if subject.user is not None and getattr(subject.user, "is_authenticated", False) else "device"
    key_hash = hash_key(f"{prefix}:{key}")
    now = timezone.now()
    user = subject.user if prefix == "user" else None
    defaults = {
        "user": user,
        "build_number": build,
        "version": (version or "")[:32],
        "last_seen_at": now,
    }
    if offered is not _UNSET:
        defaults["offered_build"] = offered.build_number if offered else None
    updated = AppVersionCheckIn.objects.filter(platform=platform, rollout_key_hash=key_hash).update(
        check_count=F("check_count") + 1, updated_at=now, **defaults
    )
    if updated:
        return None
    try:
        with transaction.atomic():
            return AppVersionCheckIn.objects.create(platform=platform, rollout_key_hash=key_hash, **defaults)
    except IntegrityError:  # гонка двух первых запросов: строку уже создали
        AppVersionCheckIn.objects.filter(platform=platform, rollout_key_hash=key_hash).update(**defaults)
        return None


# ---------------------------------------------------------------------------
# Переходы статусов
# ---------------------------------------------------------------------------
class RolloutError(ValidationError):
    pass


TRANSITIONS = {
    "start": ({ReleaseStatus.DRAFT}, ReleaseStatus.ROLLING_OUT),
    "pause": ({ReleaseStatus.ROLLING_OUT}, ReleaseStatus.PAUSED),
    "resume": ({ReleaseStatus.PAUSED}, ReleaseStatus.ROLLING_OUT),
    "complete": ({ReleaseStatus.ROLLING_OUT, ReleaseStatus.PAUSED}, ReleaseStatus.COMPLETED),
    "roll_back": (
        {ReleaseStatus.ROLLING_OUT, ReleaseStatus.PAUSED, ReleaseStatus.COMPLETED},
        ReleaseStatus.ROLLED_BACK,
    ),
}

ACTION_LABELS = {
    "create": gettext_lazy("Создан"),
    "start": gettext_lazy("Раскатка начата"),
    "set_percentage": gettext_lazy("Изменён процент"),
    "pause": gettext_lazy("Приостановлен"),
    "resume": gettext_lazy("Возобновлён"),
    "complete": gettext_lazy("Выпущен для всех"),
    "roll_back": gettext_lazy("Отозван"),
}


def _log(release: AppRelease, action: str, *, user, request, from_status, from_pct):
    AppReleaseEvent.objects.create(
        release=release,
        action=action,
        from_status=from_status or "",
        to_status=release.status,
        from_percentage=from_pct,
        to_percentage=release.rollout_percentage,
        user=user if getattr(user, "is_authenticated", False) else None,
    )
    log_action(
        user=user,
        action=f"app_release.{action}",
        target=release,
        request=request,
        extra={
            "platform": release.platform,
            "build": release.build_number,
            "status": f"{from_status}->{release.status}",
            "percentage": f"{from_pct}->{release.rollout_percentage}",
        },
    )


@transaction.atomic
def transition(release: AppRelease, action: str, *, user=None, request=None, percentage: int | None = None):
    if action == "set_percentage":
        return set_percentage(release, percentage, user=user, request=request)
    if action not in TRANSITIONS:
        raise RolloutError(_("Неизвестное действие."))
    release = AppRelease.objects.select_for_update().get(pk=release.pk)
    allowed_from, to_status = TRANSITIONS[action]
    if release.status not in allowed_from:
        raise RolloutError(
            _("Действие «%(action)s» недоступно в статусе «%(status)s».")
            % {"action": ACTION_LABELS[action], "status": release.get_status_display()}
        )
    from_status, from_pct = release.status, release.rollout_percentage
    now = timezone.now()
    if action == "start":
        if percentage is not None:
            _validate_pct(percentage)
            release.rollout_percentage = percentage
        if release.rollout_percentage == 0 and not _has_allowlist(release):
            release.rollout_percentage = ROLLOUT_PRESETS[1]
        release.started_at = now
    elif action == "complete":
        release.rollout_percentage = 100
        release.completed_at = now
    elif action == "roll_back":
        release.rolled_back_at = now
    release.status = to_status
    release.save()
    _log(release, action, user=user, request=request, from_status=from_status, from_pct=from_pct)
    return release


@transaction.atomic
def set_percentage(release: AppRelease, percentage, *, user=None, request=None) -> AppRelease:
    release = AppRelease.objects.select_for_update().get(pk=release.pk)
    pct = _validate_pct(percentage)
    if release.status in (ReleaseStatus.COMPLETED, ReleaseStatus.ROLLED_BACK):
        raise RolloutError(_("Процент меняется только у черновика, активной или приостановленной раскатки."))
    if release.status != ReleaseStatus.DRAFT and pct < release.rollout_percentage:
        raise RolloutError(
            _("Процент нельзя уменьшить (%(old)s%% → %(new)s%%): уже получившие обновление не должны его терять. "
              "Если релиз проблемный — отзовите его.")
            % {"old": release.rollout_percentage, "new": pct}
        )
    if pct == release.rollout_percentage:
        return release
    from_status, from_pct = release.status, release.rollout_percentage
    release.rollout_percentage = pct
    if pct == 100 and release.status == ReleaseStatus.ROLLING_OUT:
        release.status = ReleaseStatus.COMPLETED
        release.completed_at = timezone.now()
    release.save()
    _log(release, "set_percentage", user=user, request=request, from_status=from_status, from_pct=from_pct)
    return release


def _validate_pct(value) -> int:
    try:
        pct = int(value)
    except (TypeError, ValueError):
        raise RolloutError(_("Процент должен быть целым числом от 0 до 100.")) from None
    if not 0 <= pct <= 100:
        raise RolloutError(_("Процент должен быть целым числом от 0 до 100."))
    return pct


def _has_allowlist(release: AppRelease) -> bool:
    return bool(release.device_allowlist) or release.allowlisted_users.exists()


def available_actions(release: AppRelease) -> list[str]:
    return [action for action, (allowed, _to) in TRANSITIONS.items() if release.status in allowed]


# ---------------------------------------------------------------------------
# Статистика для админки
# ---------------------------------------------------------------------------
def adoption_stats(platform: str, *, days: int = ACTIVE_DEVICE_WINDOW_DAYS) -> dict:
    since = timezone.now() - timedelta(days=days)
    qs = AppVersionCheckIn.objects.filter(platform=platform, last_seen_at__gte=since)
    rows = list(
        qs.values("build_number")
        .annotate(devices=Count("id"), version=Max("version"))
        .order_by("-build_number")
    )
    total = sum(row["devices"] for row in rows)
    for row in rows:
        row["share"] = round(row["devices"] * 100 / total, 1) if total else 0
    return {"total": total, "builds": rows, "days": days}


def release_stats(release: AppRelease) -> dict:
    adoption = adoption_stats(release.platform)
    since = timezone.now() - timedelta(days=adoption["days"])
    qs = AppVersionCheckIn.objects.filter(platform=release.platform, last_seen_at__gte=since)
    on_build = qs.filter(build_number__gte=release.build_number).count()
    offered = qs.filter(offered_build=release.build_number).count()
    pct = release.effective_percentage if release.status != ReleaseStatus.ROLLED_BACK else 0
    below = adoption["total"] - on_build
    return {
        "adoption": adoption,
        "active_devices": adoption["total"],
        "on_build": on_build,
        "on_build_share": round(on_build * 100 / adoption["total"], 1) if adoption["total"] else 0,
        "offered": offered,
        "estimated_in_rollout": round(below * pct / 100),
        "allowlist_size": release.allowlisted_users.count() + len(release.device_allowlist),
    }
