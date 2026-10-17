"""Authentication service: PIN-code login with lockout protection."""
import re

from django.db.models import Q
from django.utils import timezone

from .exceptions import AccountBlocked, AccountInactive, InvalidPIN, PINLocked
from .models import User


def normalize_identifier(value: str) -> str:
    return re.sub(r"[^\d+]", "", value or "")


def find_user(identifier: str) -> User | None:
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    phone = normalize_identifier(identifier)
    query = Q(username=identifier)
    if phone:
        query |= Q(phone=phone)
    return User.objects.filter(query).distinct().first()


def authenticate_pin(identifier: str, pin: str) -> User:
    """Validate identifier + PIN. Raises a domain error with a stable `code`."""
    user = find_user(identifier)

    # Constant behaviour for unknown and known identifiers.
    if user is None:
        raise InvalidPIN()

    if user.is_blocked:
        raise AccountBlocked()
    if not user.is_active:
        raise AccountInactive()
    if user.is_pin_locked:
        retry_after = int((user.pin_locked_until - timezone.now()).total_seconds() + 1)
        raise PINLocked(
            detail={
                "code": "pin_locked",
                "message": "Слишком много попыток ввода ПИН-кода.",
                "retry_after": max(retry_after, 1),
            }
        )

    if not user.check_pin(pin):
        user.register_failed_pin()
        raise InvalidPIN()

    user.reset_pin_attempts()
    return user
