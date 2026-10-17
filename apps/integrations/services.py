"""Сервис синхронизации с 1С: импорт арендаторов и торговых точек (ТЗ, п. 6)."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.trade_points.models import AccessRole, Meter, TradePoint, TradePointAccess

from .clients import Base1CClient, OneCError, get_client
from .models import ExternalObject, SyncDirection, SyncEntity, SyncRun, SyncStatus

logger = logging.getLogger("apps.integrations")


def _checksum(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _external_id(payload: dict) -> str:
    return str(payload.get("id") or payload.get("code") or payload.get("Ref_Key") or "")


def _as_decimal(value, default="0"):
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, str):
        return parse_date(value[:10])
    if isinstance(value, datetime):
        return value.date()
    return None


@transaction.atomic
def import_tenant(payload: dict, sync_run: SyncRun | None = None) -> dict:
    User = get_user_model()
    external_id = _external_id(payload)
    if not external_id:
        return {"status": "skipped", "reason": "no external id"}

    phone = str(payload.get("phone") or payload.get("Phone") or "").strip()
    if not phone:
        return {"status": "skipped", "reason": "no phone", "external_id": external_id}

    defaults = {
        "first_name": payload.get("first_name") or payload.get("FirstName") or "",
        "last_name": payload.get("last_name") or payload.get("LastName") or "",
        "role": payload.get("role") or "tenant",
    }
    user, created = User.objects.update_or_create(phone=phone, defaults=defaults)
    ExternalObject.objects.update_or_create(
        entity=SyncEntity.TENANT,
        external_id=external_id,
        defaults={"internal_id": user.id, "payload": payload, "checksum": _checksum(payload)},
    )
    return {"status": "created" if created else "updated", "user_id": str(user.id)}


@transaction.atomic
def import_trade_point(payload: dict, sync_run: SyncRun | None = None) -> dict:
    User = get_user_model()
    external_id = _external_id(payload)
    if not external_id:
        return {"status": "skipped", "reason": "no external id"}

    code = str(payload.get("code") or payload.get("Code") or external_id)
    defaults = {
        "name": payload.get("name") or payload.get("Name") or f"Точка {code}",
        "block": payload.get("block") or payload.get("Block") or "",
        "place_number": str(payload.get("place_number") or payload.get("PlaceNumber") or ""),
        "address": payload.get("address") or payload.get("Address") or "",
        "external_id_1c": external_id,
        "is_active": bool(payload.get("is_active", True)),
    }
    point, created = TradePoint.objects.update_or_create(code=code, defaults=defaults)

    external = ExternalObject.objects.update_or_create(
        entity=SyncEntity.TRADE_POINT,
        external_id=external_id,
        defaults={"internal_id": point.id, "payload": payload, "checksum": _checksum(payload)},
    )[0]

    # Привязка арендатора по телефону, если он указан в выгрузке 1С.
    tenant_phone = str(payload.get("tenant_phone") or "").strip()
    if tenant_phone:
        user = User.objects.filter(phone=tenant_phone).first()
        if user:
            TradePointAccess.objects.update_or_create(
                user=user,
                trade_point=point,
                defaults={"access_role": AccessRole.TENANT, "is_active": True},
            )

    # Счётчики торговой точки (если переданы).
    meters_payload = payload.get("meters") or []
    for meter_payload in meters_payload:
        serial = str(meter_payload.get("serial_number") or meter_payload.get("Serial") or "").strip()
        if not serial:
            continue
        Meter.objects.update_or_create(
            serial_number=serial,
            defaults={
                "trade_point": point,
                "model_name": meter_payload.get("model_name") or meter_payload.get("Model") or "",
                "initial_reading": _as_decimal(meter_payload.get("initial_reading")),
                "installed_at": _parse_date(meter_payload.get("installed_at")),
                "external_id_1c": str(meter_payload.get("id") or ""),
                "is_active": True,
            },
        )

    return {
        "status": "created" if created else "updated",
        "trade_point_id": str(point.id),
        "external_object": str(external.id),
    }


def run_sync(entity: str, *, direction: str = SyncDirection.IMPORT, dry_run: bool = False,
             client: Base1CClient | None = None) -> SyncRun:
    """Запустить синхронизацию по одной сущности."""
    client = client or get_client()
    run = SyncRun.objects.create(entity=entity, direction=direction, status=SyncStatus.RUNNING)

    if direction != SyncDirection.IMPORT:
        run.status = SyncStatus.FAILED
        run.error = "Направление выгрузки в 1С не согласовано с заказчиком (ТЗ, п. 25.6)."
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error", "finished_at", "updated_at"])
        return run

    try:
        if entity == SyncEntity.TENANT:
            payloads = client.fetch_tenants()
        elif entity == SyncEntity.TRADE_POINT:
            payloads = client.fetch_trade_points()
        elif entity == SyncEntity.METER:
            payloads = client.fetch_meters()
        else:
            raise OneCError(f"Импорт сущности '{entity}' не поддерживается.")
    except OneCError as exc:
        run.status = SyncStatus.FAILED
        run.error = str(exc)
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "error", "finished_at", "updated_at"])
        return run

    stats = {"received": len(payloads), "created": 0, "updated": 0, "skipped": 0, "errors": 0}
    errors: list[str] = []

    if dry_run:
        stats["dry_run"] = True
        run.status = SyncStatus.SUCCESS
        run.stats = stats
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "stats", "finished_at", "updated_at"])
        return run

    handler = {
        SyncEntity.TENANT: import_tenant,
        SyncEntity.TRADE_POINT: import_trade_point,
        SyncEntity.METER: lambda payload, sync_run=None: {"status": "skipped", "reason": "meters imported with trade points"},
    }[entity]

    for payload in payloads:
        try:
            with transaction.atomic():
                result = handler(payload, run)
            stats[result.get("status", "skipped")] = stats.get(result.get("status", "skipped"), 0) + 1
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            errors.append(f"{_external_id(payload)}: {exc}")
            logger.exception("Ошибка импорта %s из 1С", entity)

    if errors and stats["errors"] == stats["received"]:
        run.status = SyncStatus.FAILED
    elif errors:
        run.status = SyncStatus.PARTIAL
    else:
        run.status = SyncStatus.SUCCESS

    run.stats = stats
    run.error = "\n".join(errors[:50])
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "stats", "error", "finished_at", "updated_at"])
    return run


def sync_all(*, dry_run: bool = False) -> list[SyncRun]:
    return [
        run_sync(SyncEntity.TENANT, dry_run=dry_run),
        run_sync(SyncEntity.TRADE_POINT, dry_run=dry_run),
    ]


def integration_status() -> dict:
    last_run = SyncRun.objects.order_by("-started_at").first()
    return {
        "enabled": getattr(settings, "ONES_C_ENABLED", False),
        "client": getattr(settings, "ONES_C_CLIENT", ""),
        "base_url": getattr(settings, "ONES_C_BASE_URL", ""),
        "protocol_agreed": False,
        "note": (
            "Перечень данных, направление и периодичность синхронизации, а также "
            "технический протокол определяются после получения информации о 1С (ТЗ, п. 6, 25.3–25.5)."
        ),
        "last_run": {
            "id": str(last_run.id),
            "entity": last_run.entity,
            "status": last_run.status,
            "started_at": last_run.started_at.isoformat(),
        }
        if last_run
        else None,
    }
