"""Audit trail for security-sensitive operations."""
import logging

logger = logging.getLogger("apps.audit")


def log_action(*, user=None, action: str, target=None, extra: dict | None = None, request=None):
    """Persist a line into the application log (critical operations journal)."""
    payload = {
        "action": action,
        "user_id": str(getattr(user, "id", None) or "-"),
        "user_role": getattr(user, "role", None) or "-",
        "target": f"{type(target).__name__}:{getattr(target, 'id', None)}" if target else "-",
        "ip": _client_ip(request),
        **(extra or {}),
    }
    logger.info("audit %s", payload)
    return payload


def _client_ip(request) -> str:
    if request is None:
        return "-"
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "-")
