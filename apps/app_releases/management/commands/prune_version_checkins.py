"""Удаляет устаревшие проверки версии. Запускать ежедневно (cron)."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.app_releases.models import AppVersionCheckIn


class Command(BaseCommand):
    help = "Удаляет записи AppVersionCheckIn, не обновлявшиеся дольше --days дней (по умолчанию 30)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)

    def handle(self, *args, days: int, **options):
        if days < 1:
            self.stderr.write("--days должен быть не меньше 1")
            return
        cutoff = timezone.now() - timedelta(days=days)
        deleted, _ = AppVersionCheckIn.objects.filter(last_seen_at__lt=cutoff).delete()
        self.stdout.write(f"Удалено проверок версии старше {days} дн.: {deleted}")
