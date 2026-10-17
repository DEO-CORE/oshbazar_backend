"""Small request helpers shared across apps."""
from django.utils.translation import gettext


def request_language(request) -> str:
    """Resolve the response language: explicit `lang` → user profile → header → default."""
    if request is None:
        return "ru"

    lang = request.query_params.get("lang") or (request.data or {}).get("lang")
    if not lang and getattr(request, "user", None) and request.user.is_authenticated:
        lang = getattr(request.user, "language", None)
    if not lang:
        header = request.headers.get("Accept-Language", "")
        lang = header.split(",")[0].split(";")[0].strip() if header else ""
    return lang if lang in {"ru", "ky"} else "ru"


def translate_text(payload, language: str, default: str = "") -> str:
    """Resolve a `{"ru": ..., "ky": ...}` dictionary to a plain string."""
    if payload is None:
        return default
    if isinstance(payload, str):
        return payload
    return payload.get(language) or payload.get("ru") or default


def gettext_for(language: str, message: str) -> str:
    """Select an already-localised message without forcing a locale switch."""
    return message
