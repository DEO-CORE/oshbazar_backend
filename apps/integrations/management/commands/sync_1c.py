"""Синхронизация с 1С.

    python manage.py sync_1c                 # импорт арендаторов и точек
    python manage.py sync_1c --entity trade_point --dry-run
"""
from django.core.management.base import BaseCommand

from apps.integrations.models import SyncEntity
from apps.integrations.services import run_sync, sync_all


class Command(BaseCommand):
    help = "Импортирует данные из 1С (арендаторы, торговые точки, счётчики)."

    def add_arguments(self, parser):
        parser.add_argument("--entity", choices=[e.value for e in SyncEntity], default=None)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if options["entity"]:
            runs = [run_sync(options["entity"], dry_run=options["dry_run"])]
        else:
            runs = sync_all(dry_run=options["dry_run"])

        for run in runs:
            self.stdout.write(
                f"{run.entity}: {run.get_status_display()} — {run.stats}"
                + (f" | ошибка: {run.error}" if run.error else "")
            )
