"""Общие фабрики для тестов (не тестовый модуль).

Импортируется из тестов приложений, чтобы не дублировать создание
пользователей, торговых точек, счётчиков, тарифов и периодов.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.billing.models import BillingPeriod, Tariff, period_for_date
from apps.trade_points.models import AccessRole, Meter, TradePoint, TradePointAccess
from apps.users.models import UserRole

User = get_user_model()

DEFAULT_PIN = "1234"
DEFAULT_PASSWORD = "test-pass-1234"


def make_user(phone: str = "+996700000001", pin: str = DEFAULT_PIN, **extra):
    return User.objects.create_user(phone=phone, pin=pin, **extra)


def make_tenant(phone: str = "+996700000010", pin: str = DEFAULT_PIN, **extra):
    extra.setdefault("role", UserRole.TENANT)
    return make_user(phone=phone, pin=pin, **extra)


def make_controller(phone: str = "+996700000020", pin: str = DEFAULT_PIN, **extra):
    extra.setdefault("role", UserRole.CONTROLLER)
    return make_user(phone=phone, pin=pin, **extra)


def make_admin(phone: str = "+996700000030", pin: str = DEFAULT_PIN, **extra):
    extra.setdefault("role", UserRole.ADMIN)
    extra.setdefault("is_staff", True)
    return make_user(phone=phone, pin=pin, **extra)


def make_superuser(phone: str = "+996700000040", pin: str = DEFAULT_PIN, **extra):
    return User.objects.create_superuser(phone=phone, pin=pin, **extra)


def make_trade_point(code: str = "T-001", **extra):
    extra.setdefault("name", f"Точка {code}")
    return TradePoint.objects.create(code=code, **extra)


def grant_access(user, point, role: str = AccessRole.TENANT, is_active: bool = True):
    return TradePointAccess.objects.create(
        user=user, trade_point=point, access_role=role, is_active=is_active
    )


def make_meter(point: TradePoint, serial: str = "SN-0001", initial: str | Decimal = "0", **extra):
    return Meter.objects.create(
        trade_point=point, serial_number=serial, initial_reading=Decimal(initial), **extra
    )


def make_tariff(price: str | Decimal = "3.50", valid_from: date | None = None, **extra):
    extra.setdefault("name", f"Тариф {price}")
    return Tariff.objects.create(
        price=Decimal(price),
        valid_from=valid_from or date(2020, 1, 1),
        **extra,
    )


def make_period(year: int | None = None, month: int | None = None) -> BillingPeriod:
    today = date.today()
    return period_for_date(date(year or today.year, month or today.month, 1))


def auth_client(user, **kwargs) -> APIClient:
    """APIClient с уже аутентифицированным пользователем (без похода в банк)."""
    client = APIClient()
    client.force_authenticate(user=user)
    return client
