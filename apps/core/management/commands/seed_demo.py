"""Наполнение базы демонстрационными данными.

Создаёт демо-пользователей всех ролей, тариф, торговые точки со счётчиками,
показания (начисления) и, при желании, успешный платёж — чтобы можно было
вручную проверить API и мобильное приложение.

Идемпотентна: повторный запуск не дублирует данные.

Пример:
    python manage.py seed_demo
    python manage.py seed_demo --tenants 5 --no-payments
"""
from __future__ import annotations

from decimal import Decimal
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageDraw

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.billing.models import BillingPeriod, Charge, Tariff, period_for_date
from apps.payments.services import initiate_payment
from apps.readings.models import MeterReading
from apps.readings.services import submit_reading
from apps.trade_points.models import AccessRole, Meter, TradePoint, TradePointAccess
from apps.users.models import UserRole

User = get_user_model()

DEMO_PIN = "1234"

STAFF = [
    ("+996700000040", UserRole.SUPER_ADMIN, True, True, "Супер", "Админ", "superadmin"),
    ("+996700000030", UserRole.ADMIN, False, True, "Айбек", "Администратор", "admin"),
    ("+996700000020", UserRole.CONTROLLER, False, False, "Гүлнара", "Контролёр", "controller"),
]

TENANTS = [
    ("+996700000010", "Асан", "Асанов", "А-101", "Корпус 1", "12"),
    ("+996700000011", "Бегимай", "Кыдырова", "Б-202", "Корпус 2", "7"),
    ("+996700000012", "Данияр", "Осмонов", "В-303", "Корпус 3", "21"),
]


class Command(BaseCommand):
    help = "Заполнить базу демонстрационными данными (роли, точки, счётчики, начисления)."

    def add_arguments(self, parser):
        parser.add_argument("--tenants", type=int, default=3, help="Количество арендаторов (1–3).")
        parser.add_argument("--no-payments", action="store_true", help="Не создавать демо-платёж.")

    @transaction.atomic
    def handle(self, *args, **options):
        period = self._period()
        tariff = self._tariff(period)

        self._staff()
        tenants = self._tenants(max(1, min(options["tenants"], len(TENANTS))))
        points = self._trade_points(tenants)
        self._readings(tenants, points, period)

        if not options["no_payments"] and settings.PAYMENT_SANDBOX_ENABLED:
            self._payment(tenants[0])

        self._report(tenants)

    # ------------------------------------------------------------------
    def _period(self) -> BillingPeriod:
        from django.utils import timezone

        period = period_for_date(timezone.localdate())
        self.stdout.write(f"  Период: {period.title} ({period.label})")
        return period

    def _tariff(self, period: BillingPeriod) -> Tariff:
        tariff, created = Tariff.objects.get_or_create(
            name="Демо-тариф 2026",
            defaults={
                "price": Decimal("3.50"),
                "currency": getattr(settings, "PAYMENT_CURRENCY", "KGS"),
                "valid_from": period.starts_on,
                "is_active": True,
            },
        )
        self.stdout.write(f"  Тариф: {tariff.name} — {tariff.price} {tariff.currency}"
                          f"{' (создан)' if created else ''}")
        return tariff

    def _staff(self):
        for phone, role, is_super, is_staff, first, last, note in STAFF:
            user, created = User.objects.get_or_create(
                phone=phone,
                defaults={
                    "role": role,
                    "first_name": first,
                    "last_name": last,
                    "is_staff": is_staff,
                    "is_superuser": is_super,
                    "is_active": True,
                },
            )
            if created:
                user.set_pin(DEMO_PIN)
                user.save(update_fields=["pin"])
        self.stdout.write("  Персонал: super_admin, admin, controller")

    def _tenants(self, count: int):
        tenants = []
        for phone, first, last, code, block, place in TENANTS[:count]:
            user, created = User.objects.get_or_create(
                phone=phone,
                defaults={
                    "role": UserRole.TENANT,
                    "first_name": first,
                    "last_name": last,
                    "is_active": True,
                },
            )
            if created:
                user.set_pin(DEMO_PIN)
                user.save(update_fields=["pin"])
            tenants.append((user, code, block, place))
        self.stdout.write(f"  Арендаторы: {len(tenants)}")
        return tenants

    def _trade_points(self, tenants):
        points = []
        for user, code, block, place in tenants:
            point, _ = TradePoint.objects.get_or_create(
                code=code,
                defaults={
                    "name": f"Точка {code}",
                    "block": block,
                    "place_number": place,
                    "address": "г. Ош, рынок «Ош Базар»",
                    "is_active": True,
                },
            )
            TradePointAccess.objects.get_or_create(
                user=user,
                trade_point=point,
                defaults={"access_role": AccessRole.OWNER, "is_active": True},
            )
            Meter.objects.get_or_create(
                trade_point=point,
                serial_number=f"SN-{code}",
                defaults={"initial_reading": Decimal("0"), "is_primary": True, "is_active": True},
            )
            points.append(point)
        self.stdout.write(f"  Торговые точки со счётчиками: {len(points)}")
        return points

    def _readings(self, tenants, points, period: BillingPeriod):
        submitted = 0
        for (user, *_rest), point in zip(tenants, points):
            meter = point.meters.filter(is_active=True).first()
            if meter is None:
                continue
            if MeterReading.objects.filter(meter=meter, period=period).exists():
                continue
            value = meter.initial_reading + Decimal("100")
            image = Image.new("RGB", (400, 120), "white")
            ImageDraw.Draw(image).text((20, 40), f"DEMO METER: {value} kWh", fill="black")
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            photo = SimpleUploadedFile("demo-meter.png", buffer.getvalue(), content_type="image/png")
            submit_reading(user=user, meter=meter, value=value, period=period, photo_file=photo)
            submitted += 1
        self.stdout.write(f"  Показания загружены: {submitted}")

    def _payment(self, tenant_entry):
        user = tenant_entry[0]
        charge = (
            Charge.objects.filter(owner=user, status__in=["unpaid", "partial"])
            .order_by("created_at")
            .first()
        )
        if charge is None:
            return
        if charge.paid_amount > 0:
            return
        initiate_payment(
            user=user,
            amount=charge.remaining,
            provider_code="mock",
            charge_ids=[str(charge.id)],
            description="Демо-платёж",
        )
        self.stdout.write(f"  Демо-платёж на {charge.remaining} создан и проведён")

    def _report(self, tenants):
        lines = [
            "",
            "Готово. Тестовые учётные записи (ПИН у всех: %s):" % DEMO_PIN,
            f"  super_admin — {STAFF[0][0]}",
            f"  admin       — {STAFF[1][0]}",
            f"  controller  — {STAFF[2][0]}",
        ]
        for user, code, *_ in tenants:
            lines.append(f"  tenant {code:<6} — {user.phone}")
        lines.append("")
        lines.append("Документация API: /api/v1/docs/")
        lines.append("")
        self.stdout.write(self.style.SUCCESS("\n".join(lines)))
