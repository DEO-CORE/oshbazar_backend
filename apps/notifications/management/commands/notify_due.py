"""Напоминания о приближающемся/наступившем сроке оплаты.

Запуск по расписанию (cron / systemd timer):
    python manage.py notify_due
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.billing.models import Charge, ChargeStatus
from apps.notifications.services import notify


class Command(BaseCommand):
    help = "Отправляет уведомления о приближающемся и наступившем сроке оплаты (ТЗ, п. 17)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=getattr(settings, "PAYMENT_DUE_REMINDER_DAYS", 3),
            help="За сколько дней предупреждать об оплате.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Только показать список.")

    def handle(self, *args, **options):
        days = options["days"]
        today = timezone.localdate()
        soon = today + timedelta(days=days)

        base = (
            Charge.objects.exclude(status=ChargeStatus.VOID)
            .exclude(status=ChargeStatus.PAID)
            .filter(owner__isnull=False)
            .select_related("owner", "trade_point")
        )

        due_now = base.filter(due_date=today)
        due_soon = base.filter(due_date__gt=today, due_date__lte=soon)

        counters = {"due_now": 0, "due_soon": 0}
        for charge in due_now:
            self._notify(charge, "payment_due_now", options["dry_run"], counters, "due_now")
        for charge in due_soon:
            self._notify(charge, "payment_due_soon", options["dry_run"], counters, "due_soon")

        self.stdout.write(
            self.style.SUCCESS(
                f"Срок сегодня: {counters['due_now']}, приближается: {counters['due_soon']}"
                + (" (dry-run)" if options["dry_run"] else "")
            )
        )

    def _notify(self, charge, notification_type, dry_run, counters, key):
        counters[key] += 1
        if dry_run:
            self.stdout.write(f"  {charge.owner} — {charge.trade_point.code} — {charge.remaining}")
            return
        notify(
            charge.owner,
            notification_type,
            {
                "amount": f"{charge.remaining:.2f}",
                "currency": settings.PAYMENT_CURRENCY,
                "point": charge.trade_point.code,
                "due_date": charge.due_date.isoformat() if charge.due_date else "—",
            },
        )
