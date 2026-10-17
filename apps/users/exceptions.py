"""Domain errors for the authentication flow."""
from django.utils.translation import gettext as _
from rest_framework import exceptions


class InvalidPIN(exceptions.APIException):
    status_code = 400
    default_code = "invalid_pin"
    default_detail = _("Неверный ПИН-код.")


class PINLocked(exceptions.APIException):
    status_code = 429
    default_code = "pin_locked"
    default_detail = _("Слишком много попыток ввода ПИН-кода. Повторите попытку позже.")


class AccountBlocked(exceptions.APIException):
    status_code = 403
    default_code = "account_blocked"
    default_detail = _("Учётная запись заблокирована.")


class AccountInactive(exceptions.APIException):
    status_code = 403
    default_code = "account_inactive"
    default_detail = _("Учётная запись деактивирована.")
