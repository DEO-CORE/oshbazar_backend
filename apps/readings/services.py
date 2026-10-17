"""Сервис передачи и проверки показаний (ТЗ, п. 8–9)."""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.models import BillingPeriod, Charge, ChargeStatus
from apps.billing.services import current_period, money, tariff_for_period
from apps.trade_points.models import Meter, TradePoint

from .models import MeterReading, ReadingPhoto, ReadingSource, ReadingStatus


def previous_value_for(meter: Meter, period: BillingPeriod) -> Decimal:
    """Показание на начало периода: последнее принятое значение за более ранний период."""
    last = (
        MeterReading.objects.filter(meter=meter, status=ReadingStatus.APPROVED, period__lt=period)
        .order_by("-period__year", "-period__month")
        .first()
    )
    if last:
        return Decimal(last.value)
    return Decimal(meter.initial_reading)


def ensure_open_period(period: BillingPeriod | None = None) -> BillingPeriod:
    period = period or current_period()
    if not period.is_open:
        raise DjangoValidationError({"period": f"Расчётный период {period.label} закрыт для показаний."})
    return period


def estimate(meter: Meter, value: Decimal, period: BillingPeriod | None = None) -> dict:
    """Предварительный расчёт до отправки показания (ТЗ, п. 8, шаг 5)."""
    period = period or current_period()
    previous = previous_value_for(meter, period)
    consumption = max(Decimal(value) - previous, Decimal("0"))
    tariff = tariff_for_period(period)
    price = tariff.price if tariff else Decimal("0")
    return {
        "period": {"id": str(period.id), "label": period.label, "title": period.title,
                   "due_date": period.due_date.isoformat(), "is_open": period.is_open},
        "meter": str(meter.id),
        "previous_value": str(previous),
        "value": str(value),
        "consumption": str(consumption),
        "tariff": {"id": str(tariff.id) if tariff else None, "price": str(price),
                   "currency": tariff.currency if tariff else settings.PAYMENT_CURRENCY},
        "estimated_amount": str(money(consumption * price)),
        "decreases": Decimal(value) < previous,
        "photo_required": bool(getattr(settings, "READING_PHOTO_REQUIRED", False)),
    }


def _validate_photo(file_obj) -> None:
    if file_obj is None:
        return
    allowed = getattr(settings, "READING_PHOTO_ALLOWED_TYPES", [])
    content_type = getattr(file_obj, "content_type", "") or ""
    if allowed and content_type not in allowed:
        raise DjangoValidationError(
            {"photo": f"Недопустимый тип файла: {content_type}. Разрешены: {', '.join(allowed)}."}
        )
    max_bytes = int(getattr(settings, "READING_PHOTO_MAX_MB", 10)) * 1024 * 1024
    size = getattr(file_obj, "size", 0)
    if size and size > max_bytes:
        raise DjangoValidationError(
            {"photo": f"Фотография больше {settings.READING_PHOTO_MAX_MB} МБ."}
        )
    try:
        from PIL import Image

        file_obj.seek(0)
        with Image.open(file_obj) as image:
            image.verify()
        file_obj.seek(0)
    except Exception as exc:  # noqa: BLE001 - any decode failure means a broken photo
        raise DjangoValidationError({"photo": "Файл не является корректным изображением."}) from exc


@transaction.atomic
def submit_reading(
    *,
    user,
    meter: Meter,
    value: Decimal,
    photo_file=None,
    period: BillingPeriod | None = None,
    source: str = ReadingSource.TENANT,
) -> MeterReading:
    """Создать показание, приложить фото и (при авто-принятии) сформировать начисление."""
    trade_point = meter.trade_point
    period = ensure_open_period(period)

    existing = MeterReading.objects.filter(meter=meter, period=period).exclude(
        status=ReadingStatus.REJECTED
    ).first()
    if existing:
        raise DjangoValidationError(
            {"period": f"Показание за период {period.label} уже передано."}
        )

    reading = MeterReading(
        meter=meter,
        trade_point=trade_point,
        period=period,
        submitted_by=user,
        previous_value=previous_value_for(meter, period),
        value=value,
        source=source,
    )
    reading.full_clean(exclude=["photo", "status"])

    if photo_file is not None:
        _validate_photo(photo_file)
        photo = ReadingPhoto.objects.create(
            file=photo_file,
            content_type=getattr(photo_file, "content_type", "") or "",
            size_bytes=getattr(photo_file, "size", 0) or 0,
            uploaded_by=user,
            trade_point=trade_point,
            meter=meter,
            original_value=value,
        )
        reading.photo = photo

    require_approval = getattr(settings, "READINGS_REQUIRE_APPROVAL", False)
    reading.status = ReadingStatus.PENDING if require_approval else ReadingStatus.APPROVED

    try:
        reading.save()
    except IntegrityError as exc:
        raise DjangoValidationError(
            {"period": f"Показание за период {period.label} уже передано."}
        ) from exc

    _notify_staff_new_reading(reading)

    if reading.status == ReadingStatus.APPROVED:
        _finalize_approved(reading)

    return reading


def attach_photo(reading: MeterReading, photo_file, user) -> ReadingPhoto:
    """Приложить/заменить фотографию к ещё не проверенному показанию."""
    if reading.status not in {ReadingStatus.PENDING, ReadingStatus.APPROVED}:
        raise DjangoValidationError({"photo": "К отклонённому показанию нельзя приложить фото."})
    if reading.photo_id:
        reading.photo.delete()
    _validate_photo(photo_file)
    photo = ReadingPhoto.objects.create(
        file=photo_file,
        content_type=getattr(photo_file, "content_type", "") or "",
        size_bytes=getattr(photo_file, "size", 0) or 0,
        uploaded_by=user,
        trade_point=reading.trade_point,
        meter=reading.meter,
        original_value=reading.value,
    )
    reading.photo = photo
    reading.save(update_fields=["photo", "updated_at"])
    return photo


def review_reading(
    reading: MeterReading, *, action: str, actor, reason: str = ""
) -> MeterReading:
    """Подтверждение/отклонение показания контролёром или администратором."""
    if reading.status != ReadingStatus.PENDING:
        raise DjangoValidationError({"status": "Показание уже проверено."})

    reading.reviewed_by = actor
    reading.reviewed_at = timezone.now()

    if action == "approve":
        reading.status = ReadingStatus.APPROVED
        reading.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
        _finalize_approved(reading)
    else:
        reading.status = ReadingStatus.REJECTED
        reading.rejection_reason = reason or "Отклонено контролёром"
        reading.save(
            update_fields=["status", "rejection_reason", "reviewed_by", "reviewed_at", "updated_at"]
        )
        from apps.notifications.services import notify

        notify(
            reading.submitted_by,
            "reading_rejected",
            {
                "value": f"{reading.value}",
                "point": str(reading.trade_point),
                "reason": reading.rejection_reason,
            },
        )
    return reading


def _finalize_approved(reading: MeterReading) -> None:
    from apps.billing.services import create_charge_for_reading
    from apps.notifications.services import notify

    create_charge_for_reading(reading)
    notify(
        reading.submitted_by,
        "reading_received",
        {
            "value": f"{reading.value}",
            "point": str(reading.trade_point),
            "period": reading.period.label,
        },
    )


def _notify_staff_new_reading(reading: MeterReading) -> None:
    from apps.notifications.services import notify
    from apps.users.models import UserRole

    from django.contrib.auth import get_user_model

    staff = get_user_model().objects.filter(
        role__in=[UserRole.CONTROLLER, UserRole.ADMIN, UserRole.SUPER_ADMIN], is_active=True
    ).exclude(pk=reading.submitted_by_id)
    for user in staff[:200]:
        notify(
            user,
            "system",
            {
                "message": (
                    f"Новые показания: {reading.trade_point.code} — {reading.value} кВт⋅ч "
                    f"({reading.period.label})"
                )
            },
        )
