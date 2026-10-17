"""Сервис уведомлений: тексты на ru/ky, доставка push."""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from apps.core.texts import NOTIFICATION_TEXTS
from apps.core.utils import translate_text

from .models import Notification, NotificationType
from .push import get_push_backend

logger = logging.getLogger("apps.notifications")


def render_texts(notification_type: str, params: dict, language: str) -> tuple[str, str]:
    template = NOTIFICATION_TEXTS.get(notification_type) or NOTIFICATION_TEXTS["system"]
    title = translate_text(template["title"], language, default="Ош Базар")
    body = translate_text(template["body"], language, default="")
    safe_params = {k: v for k, v in (params or {}).items() if isinstance(v, (str, int, float))}
    try:
        title = title.format(**safe_params)
        body = body.format(**safe_params)
    except (KeyError, IndexError, ValueError):
        logger.warning("Некорректные параметры уведомления %s: %s", notification_type, safe_params)
    return title, body


def notify(
    user,
    notification_type: str,
    params: dict | None = None,
    *,
    title: str | None = None,
    body: str | None = None,
    push: bool = True,
    save: bool = True,
) -> Notification | None:
    """Создать уведомление и (опционально) отправить push пользователю."""
    if user is None:
        return None
    params = params or {}
    language = getattr(user, "language", "ru") or "ru"
    rendered_title, rendered_body = render_texts(notification_type, params, language)

    notification = Notification(
        user=user,
        type=notification_type if notification_type in NotificationType.values else NotificationType.SYSTEM,
        title=(title or rendered_title)[:255],
        body=body or rendered_body,
        params=params,
    )
    if save:
        notification.save()

    if push and getattr(settings, "PUSH_ENABLED", False):
        _send_push(user, notification)

    return notification


def _send_push(user, notification: Notification) -> None:
    tokens = list(user.device_tokens.filter(is_active=True))
    if not tokens:
        return
    backend = get_push_backend()
    errors = []
    sent = False
    for device in tokens:
        try:
            ok = backend.send(
                token=device.token,
                title=notification.title,
                body=notification.body,
                data={"type": notification.type, "notification_id": str(notification.id)},
            )
            sent = sent or ok
            if not ok:
                errors.append(f"{device.platform}: отправка отклонена")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{device.platform}: {exc}")
            logger.exception("Ошибка отправки push пользователю %s", user.pk)
    notification.push_sent = sent
    notification.push_error = "; ".join(errors)[:255]
    notification.save(update_fields=["push_sent", "push_error", "updated_at"])


def notify_many(users, notification_type: str, params: dict | None = None) -> int:
    count = 0
    for user in users:
        notify(user, notification_type, params)
        count += 1
    return count


def mark_read(notification: Notification) -> Notification:
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at", "updated_at"])
    return notification


def mark_all_read(user) -> int:
    return user.notifications.filter(is_read=False).update(is_read=True, read_at=timezone.now())
