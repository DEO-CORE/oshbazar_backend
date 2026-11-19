"""Создать три рынка со схемами и (по желанию) расставить точки по местам.

Примеры:
    python manage.py seed_markets
    python manage.py seed_markets --reset-layout        # перерисовать схемы по умолчанию
    python manage.py seed_markets --place --fill --default-market osh-bazary
"""
from django.core.management.base import BaseCommand

from apps.trade_points.market_map import auto_place, seed_markets


class Command(BaseCommand):
    help = "Создать рынки «Ош Базары», «Бекере Базары», «Жибек Жолу Базары» со схемами для карты."

    def add_arguments(self, parser):
        parser.add_argument("--reset-layout", action="store_true", help="Заменить схемы существующих рынков.")
        parser.add_argument("--place", action="store_true", help="Сразу расставить точки (см. place_trade_points).")
        parser.add_argument("--fill", action="store_true", help="Неподходящие точки — на первые свободные места.")
        parser.add_argument("--default-market", default=None, help="slug рынка для точек без рынка.")

    def handle(self, *args, **options):
        created, updated = seed_markets(reset_layout=options["reset_layout"])
        self.stdout.write(self.style.SUCCESS(f"Рынков создано: {created}, схем обновлено: {updated}"))
        if options["place"]:
            result = auto_place(default_market=options["default_market"], fill=options["fill"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"Расставлено точек: {result.placed} (по номеру места: {result.by_number}, "
                    f"на свободные: {result.by_fill}), пропущено: {result.skipped}"
                )
            )
