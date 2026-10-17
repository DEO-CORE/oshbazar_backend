"""Uniform JSON error responses for the mobile client."""
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.encoding import force_str
from django.utils.translation import gettext as _
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


def _field_of(detail, field=None):
    return field


def _flatten(detail, field=None):
    """Normalize any exception detail into a list of {code, message, field}."""
    items = []
    if isinstance(detail, dict):
        if "message" in detail and "code" in detail:
            item = {"code": str(detail["code"]), "message": str(detail["message"]), "field": field}
            for key, value in detail.items():
                if key not in {"code", "message"}:
                    item[key] = value
            return [item]
        for key, value in detail.items():
            items.extend(_flatten(value, field=str(key)))
        return items

    if isinstance(detail, (list, tuple)):
        for value in detail:
            items.extend(_flatten(value, field=field))
        return items

    if isinstance(detail, exceptions.ErrorDetail):
        code = detail.code if getattr(detail, "code", None) else "error"
    elif isinstance(detail, str):
        code, detail = "error", detail
    else:
        code = "error"
        detail = force_str(detail)

    items.append({"code": str(code), "message": str(detail), "field": field})
    return items


def api_exception_handler(exc, context):
    if isinstance(exc, DjangoValidationError):
        detail = getattr(exc, "message_dict", None) or getattr(exc, "messages", None) or str(exc)
        return Response({"errors": _flatten(detail)}, status=status.HTTP_400_BAD_REQUEST)

    response = drf_exception_handler(exc, context)

    if response is None:
        if isinstance(exc, DjangoPermissionDenied):
            return Response(
                {
                    "errors": [
                        {
                            "code": "permission_denied",
                            "message": _("У вас нет прав для выполнения этого действия."),
                            "field": None,
                        }
                    ]
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    if isinstance(exc, exceptions.AuthenticationFailed):
        code = "authentication_failed"
    elif isinstance(exc, exceptions.NotAuthenticated):
        code = "not_authenticated"
    elif isinstance(exc, exceptions.PermissionDenied):
        code = "permission_denied"
    elif isinstance(exc, exceptions.NotFound):
        code = "not_found"
    elif isinstance(exc, exceptions.Throttled):
        code = "throttled"
    elif isinstance(exc, exceptions.ValidationError):
        code = "validation_error"
    elif isinstance(exc, exceptions.APIException):
        code = getattr(exc, "default_code", "error")
    else:
        code = "error"

    if not isinstance(response.data, dict) or "errors" not in response.data:
        detail = response.data.get("detail", response.data) if isinstance(response.data, dict) else response.data
        response.data = {"errors": _flatten(detail)}
        for item in response.data["errors"]:
            item.setdefault("code", code)

    return response
