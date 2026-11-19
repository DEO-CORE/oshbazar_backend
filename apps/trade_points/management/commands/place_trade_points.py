"""Расставить торговые точки по ячейкам схем рынков.

Правила: блок схемы ищется по полю «корпус / ряд» точки (id, name или aliases
блока), номер ячейки — по цифрам из «номер места»: «Ряд А» + «12» → «А-12».

Примеры:
    python manage.py place_trade_points --dry-run
    python manage.py place_trade_points --default-market osh-bazary --fill
    python manage.py place_trade_points --market bekere --reset --fill
"""
from django.core.management.base import BaseCommand, CommandError

from apps.trade_points.market_map import auto_place
from apps.trade_points.models import Market


class Command(BaseCommand):
    help = "Автоматически разместить торговые точки на карте рынков."

    def add_arguments(self, parser):
        parser.add_argument("--market", action="append", dest="markets", help="slug рынка (можно несколько).")
        parser.add_argument("--default-market", default=None, help="slug рынка для точек без рынка и без совпадений.")
        parser.add_argument("--fill", action="store_true", help="Неподходящие точки — на первые свободные места.")
        parser.add_argument("--reset", action="store_true", help="Сначала снять все точки с мест выбранных рынков.")
        parser.add_argument("--dry-run", action="store_true", help="Только показать результат, ничего не менять.")

    def handle(self, *args, **options):
        known = set(Market.objects.values_list("slug", flat=True))
        for slug in (options["markets"] or []) + ([options["default_market"]] if options["default_market"] else []):
            if slug not in known:
                raise CommandError(f"Рынок «{slug}» не найден. Есть: {', '.join(sorted(known)) or '—'}")
        result = auto_place(
            market_slugs=options["markets"],
            default_market=options["default_market"],
            fill=options["fill"],
            reset=options["reset"],
            dry_run=options["dry_run"],
        )
        prefix = "[dry-run] " if options["dry_run"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}Расставлено: {result.placed} (по номеру места: {result.by_number}, "
                f"на свободные места: {result.by_fill}); снято: {result.reset}; пропущено: {result.skipped}"
            )
        )
        for line in result.details[:20]:
            self.stdout.write(f"  {line}")
