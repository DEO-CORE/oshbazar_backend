"""Напоминания о задолженности.

Периодичность напоминаний определяется заказчиком (ТЗ, п. 17, 25.23).
Запуск:
    python manage.py notify_debtors [--min-amount 100]
"""
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Count, Sum

from apps.billing.models import Charge, ChargeStatus
from apps.notifications.services import notify


class Command(BaseCommand):
    help = "Рассылает напоминания о задолженности арендаторам (ТЗ, п. 12, 17)."

    def add_arguments(self, parser):
        parser.add_argument("--min-amount", type=Decimal, default=Decimal("0"))
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        min_amount = options["min_amount"]
        User = get_user_model()
        debtors = (
            Charge.objects.exclude(status=ChargeStatus.VOID)
            .filter(owner__isnull=False)
            .values("owner")
            .annotate(
                total=Sum("amount"),
                paid=Sum("paid_amount"),
                periods=Count("period", distinct=True),
            )
        )

        sent = 0
        for row in debtors:
            remaining = (row["total"] or Decimal("0")) - (row["paid"] or Decimal("0"))
            if remaining <= Decimal("0") or remaining < min_amount:
                continue
            user = User.objects.filter(pk=row["owner"]).first()
            if user is None:
                continue
            if options["dry_run"]:
                self.stdout.write(f"  {user} — {remaining}")
                sent += 1
                continue
            notify(
                user,
                "debt_reminder",
                {
                    "amount": f"{remaining:.2f}",
                    "currency": settings.PAYMENT_CURRENCY,
                    "periods": row["periods"],
                },
            )
            sent += 1

        self.stdout.write(
            self.style.SUCCESS(f"Напоминаний отправлено: {sent}" + (" (dry-run)" if options["dry_run"] else ""))
        )
