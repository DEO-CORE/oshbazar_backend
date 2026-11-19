from decimal import Decimal
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.billing.models import Charge
from apps.core.testing import (
    grant_access,
    make_admin,
    make_controller,
    make_meter,
    make_period,
    make_superuser,
    make_tenant,
    make_trade_point,
)
from apps.readings.models import MeterReading, ReadingStatus
from apps.trade_points.market_layouts import DEFAULT_MARKETS
from apps.trade_points.market_map import (
    BlockGrid,
    auto_place,
    build_map_payload,
    layout_slot_keys,
    slot_center,
    validate_layout,
)
from apps.trade_points.models import Market, TradePoint

DATA_URL = "/admin/market-map/data/"
PAGE_URL = "/admin/market-map/"


class LayoutTests(TestCase):
    def test_markets_seeded_by_migration(self):
        markets = list(Market.objects.order_by("sort_order"))
        self.assertEqual([m.slug for m in markets], ["osh-bazary", "bekere", "zhibek-zholu"])
        self.assertEqual(markets[0].parking_places, 293)
        self.assertEqual(markets[1].parking_places, 800)
        for market in markets:
            validate_layout(market.layout)
            keys = layout_slot_keys(market.layout)
            self.assertGreater(len(keys), 300, market.slug)
            self.assertEqual(len(keys), len(set(keys)), f"дубли ключей в {market.slug}")
            self.assertEqual(market.slot_count, len(keys))

    def test_grid_numbering_and_aisles(self):
        grid = BlockGrid.from_layout(
            {"id": "X", "cols": 4, "rows": 2, "slot": [2, 3], "gap": [0, 0], "aisle_every": 2, "aisle": 5}
        )
        slots = {n: (x, y) for n, x, y in grid.slots()}
        # По вертикальным рядам: 1–2 в первом столбце, 3–4 во втором, затем проход 5 м.
        self.assertEqual(slots[1], (0, 0))
        self.assertEqual(slots[2], (0, 3))
        self.assertEqual(slots[3], (2, 0))
        self.assertEqual(slots[5], (9, 0))
        self.assertEqual(grid.width, 2 * 4 + 5)
        rows = BlockGrid.from_layout({"id": "Y", "cols": 3, "rows": 2, "order": "rows", "gap": [0, 0], "slot": [1, 1]})
        self.assertEqual([n for n, x, y in rows.slots() if y == 0], [1, 2, 3])

    def test_slot_center_uses_offset_and_rotation(self):
        layout = {"offset": [100, 50], "blocks": [{"id": "A", "x": 10, "y": 0, "rotate": 90, "cols": 1, "rows": 1,
                                                    "slot": [2, 4], "gap": [0, 0]}]}
        x, y = slot_center(layout, "A-1")
        self.assertAlmostEqual(x, 100 + 10 - 2)
        self.assertAlmostEqual(y, 50 + 1)
        self.assertIsNone(slot_center(layout, "A-2"))

    def test_validate_layout_rejects_bad_schema(self):
        with self.assertRaises(ValidationError):
            validate_layout({"blocks": [{"id": "A", "cols": 2, "rows": 2}, {"id": "A", "cols": 0, "rows": 1}]})
        with self.assertRaises(ValidationError):
            validate_layout({"zones": [{"kind": "castle"}]})
        with self.assertRaises(ValidationError):
            Market(name="X", slug="x", layout={"outline": [[0, 0]]}).full_clean()

    def test_slot_unique_per_market(self):
        market = Market.objects.get(slug="bekere")
        make_trade_point("P-1", market=market, map_slot="К1-1")
        make_trade_point("P-2", market=Market.objects.get(slug="zhibek-zholu"), map_slot="К1-1")
        make_trade_point("P-3", market=market, map_slot="")
        make_trade_point("P-4", market=market, map_slot="")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_trade_point("P-5", market=market, map_slot="К1-1")


class SeedAndPlacementTests(TestCase):
    def test_seed_markets_is_idempotent_and_resets_layout(self):
        market = Market.objects.get(slug="osh-bazary")
        market.name = "Ош Базары (переименован)"
        market.layout = {"blocks": []}
        market.save()
        out = StringIO()
        call_command("seed_markets", stdout=out)
        self.assertIn("Рынков создано: 0", out.getvalue())
        self.assertEqual(Market.objects.count(), len(DEFAULT_MARKETS))
        market.refresh_from_db()
        self.assertEqual(market.layout, {"blocks": []})  # без --reset-layout схема не трогается
        call_command("seed_markets", "--reset-layout", stdout=StringIO())
        market.refresh_from_db()
        self.assertEqual(market.name, "Ош Базары (переименован)")  # имя, заданное админом, сохраняется
        self.assertTrue(layout_slot_keys(market.layout))

    def test_seed_markets_recreates_deleted(self):
        Market.objects.all().delete()
        call_command("seed_markets", stdout=StringIO())
        self.assertEqual(Market.objects.count(), 3)

    def test_place_by_block_alias_and_number(self):
        a12 = make_trade_point("А-12", block="Корпус 1", place_number="12")
        k3 = make_trade_point("K-3", block="Ряд К3", place_number="№ 5")
        nomatch = make_trade_point("Z-1", block="Неизвестный", place_number="1")
        out = StringIO()
        call_command("place_trade_points", stdout=out)
        a12.refresh_from_db(); k3.refresh_from_db(); nomatch.refresh_from_db()
        self.assertEqual((a12.market.slug, a12.map_slot), ("osh-bazary", "А-12"))
        self.assertEqual((k3.market.slug, k3.map_slot), ("bekere", "К3-5"))
        self.assertIsNone(nomatch.market)
        self.assertIn("Расставлено: 2", out.getvalue())

    def test_conflict_and_fill(self):
        osh = Market.objects.get(slug="osh-bazary")
        make_trade_point("FIRST", market=osh, map_slot="А-1")
        second = make_trade_point("SECOND", block="А", place_number="1")
        result = auto_place()
        second.refresh_from_db()
        self.assertEqual(second.map_slot, "")
        self.assertEqual(second.market, osh)  # рынок определён по блоку, место занято
        self.assertEqual(result.skipped, 1)
        result = auto_place(fill=True)
        second.refresh_from_db()
        self.assertEqual(second.map_slot, "А-2")
        self.assertEqual(result.by_fill, 1)

    def test_default_market_dry_run_and_reset(self):
        point = make_trade_point("D-1", place_number="3")
        call_command("place_trade_points", "--default-market", "zhibek-zholu", "--fill", "--dry-run", stdout=StringIO())
        point.refresh_from_db()
        self.assertIsNone(point.market)
        call_command("place_trade_points", "--default-market", "zhibek-zholu", "--fill", stdout=StringIO())
        point.refresh_from_db()
        self.assertEqual(point.market.slug, "zhibek-zholu")
        self.assertEqual(point.map_slot, layout_slot_keys(point.market.layout)[0])
        call_command("place_trade_points", "--market", "zhibek-zholu", "--reset", stdout=StringIO())
        point.refresh_from_db()
        self.assertEqual(point.map_slot, "")

    def test_unknown_market_is_error(self):
        with self.assertRaises(CommandError):
            call_command("place_trade_points", "--market", "nope", stdout=StringIO())

    def test_seed_demo_places_points_on_map(self):
        call_command("seed_demo", "--no-payments", "--map-demo", "20", stdout=StringIO())
        self.assertGreaterEqual(TradePoint.objects.exclude(map_slot="").count(), 20)
        self.assertEqual(TradePoint.objects.get(code="А-101").map_slot, "А-12")


@override_settings(ALLOWED_HOSTS=["*"], READING_PHOTO_REQUIRED=False)
class MapEndpointTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.period = make_period()
        self.osh = Market.objects.get(slug="osh-bazary")
        self.tenant = make_tenant(first_name="Асан", last_name="Асанов")

    def _point(self, code, slot, *, reading=None, debt=None, active=True):
        point = make_trade_point(code, market=self.osh, map_slot=slot, is_active=active)
        meter = make_meter(point, serial=f"SN-{code}")
        grant_access(self.tenant, point)
        if reading:
            r = MeterReading.objects.create(
                meter=meter, trade_point=point, period=self.period, submitted_by=self.tenant,
                value=Decimal("100"), status=reading,
            )
            if debt is not None:
                Charge.objects.create(
                    period=self.period, trade_point=point, meter=meter, source_reading_id=r.id,
                    amount=Decimal("350"), paid_amount=Decimal("350") - Decimal(debt),
                )
        return point

    def test_permissions(self):
        self.assertEqual(self.client.get(DATA_URL).status_code, 302)  # аноним → вход
        self.assertEqual(self.client.get(PAGE_URL).status_code, 302)
        controller = make_controller(is_staff=True)
        self.client.force_login(controller)
        response = self.client.get(DATA_URL)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["errors"][0]["code"], "forbidden")
        self.assertEqual(self.client.get(PAGE_URL).status_code, 403)
        self.client.force_login(self.tenant)  # не персонал
        self.assertEqual(self.client.get(DATA_URL).status_code, 302)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(DATA_URL).status_code, 200)
        self.client.force_login(make_superuser())
        self.assertEqual(self.client.get(DATA_URL).status_code, 200)

    def test_payload_statuses_and_summary(self):
        self._point("PAID", "А-1", reading=ReadingStatus.APPROVED, debt="0")
        self._point("DEBT", "А-2", reading=ReadingStatus.APPROVED, debt="200")
        self._point("PEND", "А-3", reading=ReadingStatus.PENDING)
        self._point("MISS", "А-4")
        self._point("OFF", "А-5", active=False)
        self._point("NOWHERE", "")
        self._point("BADSLOT", "НЕТ-999")
        self.client.force_login(self.admin)
        data = self.client.get(DATA_URL).json()

        by_code = {p["code"]: p for p in data["points"]}
        self.assertEqual(by_code["PAID"]["status"], "paid")
        self.assertEqual(by_code["DEBT"]["status"], "debt")
        self.assertEqual(by_code["DEBT"]["debt"], "200.00")
        self.assertGreaterEqual(by_code["DEBT"]["debt_level"], 1)
        self.assertEqual(by_code["PEND"]["status"], "pending")
        self.assertEqual(by_code["MISS"]["status"], "missing")
        self.assertEqual(by_code["OFF"]["status"], "inactive")
        self.assertIsNone(by_code["NOWHERE"]["slot"])
        self.assertIsNone(by_code["BADSLOT"]["slot"])  # ключ, которого нет на схеме
        self.assertEqual(by_code["PAID"]["tenants"][0]["phone"], self.tenant.phone)
        self.assertEqual(by_code["PAID"]["meter"], "SN-PAID")
        self.assertEqual(data["unplaced"], 2)

        self.assertEqual([m["slug"] for m in data["markets"]], ["osh-bazary", "bekere", "zhibek-zholu"])
        osh = data["summary"]["osh-bazary"]
        self.assertEqual(osh["points"], 7)
        self.assertEqual(osh["placed"], 5)
        self.assertEqual(osh["debt_total"], "200.00")
        self.assertEqual(osh["debtors"], 1)
        self.assertEqual(osh["statuses"]["free"], osh["slots"] - 5)
        self.assertEqual(osh["statuses"]["paid"], 1)
        # Показания поданы у 3 из 6 активных точек.
        self.assertEqual(osh["readings_submitted"], 3)
        self.assertEqual(osh["readings_pct"], 50.0)
        self.assertEqual(data["summary"]["bekere"]["points"], 0)
        self.assertEqual(data["summary"]["all"]["slots"], sum(s["slots"] for k, s in data["summary"].items() if k != "all"))
        block = data["markets"][0]["blocks"][0]
        self.assertEqual(block["id"], "А")
        self.assertEqual(block["slots"][0][0], 1)
        self.assertEqual(len(data["bounds"]), 4)

    def test_no_n_plus_one(self):
        self.client.force_login(self.admin)
        for i in range(3):
            self._point(f"Q{i}", f"А-{i + 1}", reading=ReadingStatus.APPROVED, debt="10")
        build_map_payload()  # прогрев: период создаётся при первом запросе
        with CaptureQueriesContext(connection) as small:
            build_map_payload()
        for i in range(3, 15):
            self._point(f"Q{i}", f"А-{i + 1}", reading=ReadingStatus.PENDING)
        with CaptureQueriesContext(connection) as big:
            build_map_payload()
        self.assertEqual(len(small.captured_queries), len(big.captured_queries))
        self.assertLessEqual(len(big.captured_queries), 8)

    def test_point_detail(self):
        point = self._point("DET", "А-7", reading=ReadingStatus.APPROVED, debt="120.50")
        self.client.force_login(self.admin)
        url = reverse("market_map:point", args=[point.pk])
        data = self.client.get(url).json()
        self.assertEqual(data["code"], "DET")
        self.assertEqual(data["status"], "debt")
        self.assertEqual(data["debt"], "120.50")
        self.assertEqual(data["unpaid_charges"], 1)
        self.assertEqual(data["tenants"][0]["name"], "Асанов Асан")
        self.assertEqual(data["meters"][0]["serial"], "SN-DET")
        self.assertEqual(data["last_reading"]["value"], "100.00")
        self.assertIn(str(point.pk), data["links"]["change"])
        self.assertIn("?q=DET", data["links"]["charges"])
        self.client.force_login(make_controller(is_staff=True))
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_page_renders_and_links(self):
        self.client.force_login(self.admin)
        response = self.client.get(PAGE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="mm-canvas"')
        self.assertContains(response, 'id="mm-ping"')
        self.assertNotContains(response, "<svg id=\"mm-svg\"")
        self.assertContains(response, "market_map/map.js")
        self.assertContains(response, DATA_URL)
        self.assertContains(response, "Бекере Базары")
        self.client.force_login(make_superuser())
        changelist = self.client.get(reverse("admin:trade_points_tradepoint_changelist"))
        self.assertContains(changelist, PAGE_URL)

    def test_market_admin_pages(self):
        self.client.force_login(make_superuser())
        self._point("ADM", "А-1")
        response = self.client.get(reverse("admin:trade_points_market_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"?market__id__exact={self.osh.pk}")
        self.assertEqual(self.client.get(reverse("admin:trade_points_market_change", args=[self.osh.pk])).status_code, 200)
        filtered = self.client.get(reverse("admin:trade_points_tradepoint_changelist") + f"?market__id__exact={self.osh.pk}")
        self.assertContains(filtered, "ADM")


class ReviewFixesTests(TestCase):
    """Регрессии по итогам ревью: seed_demo, замороженная миграция, urlencode, права меню."""

    def test_seed_demo_does_not_rehome_real_points(self):
        real = make_trade_point("REAL-1", place_number="5")  # без рынка и без блока
        call_command("seed_demo", "--no-payments", stdout=StringIO())
        real.refresh_from_db()
        self.assertIsNone(real.market)
        self.assertEqual(real.map_slot, "")
        self.assertEqual(TradePoint.objects.get(code="А-101").map_slot, "А-12")  # демо-точка размещена

    def test_auto_place_point_ids_respects_other_points(self):
        osh = Market.objects.get(slug="osh-bazary")
        make_trade_point("OWNER", market=osh, map_slot="А-1")
        target = make_trade_point("T", block="А", place_number="1")
        other = make_trade_point("OTHER", block="А", place_number="3")
        auto_place(point_ids=[target.pk], fill=True)
        target.refresh_from_db(); other.refresh_from_db()
        self.assertEqual(target.map_slot, "А-2")  # А-1 занято точкой вне выборки
        self.assertEqual(other.map_slot, "")  # не входила в point_ids — не тронута

    def test_seed_migration_is_frozen(self):
        import importlib
        import inspect

        module = importlib.import_module("apps.trade_points.migrations.0004_seed_markets")
        source = inspect.getsource(module)
        self.assertNotIn("market_layouts", source.split('"""', 2)[2])  # только в docstring
        self.assertNotIn("import apps", source)
        self.assertEqual([m["slug"] for m in module.MARKETS], ["osh-bazary", "bekere", "zhibek-zholu"])
        for spec in module.MARKETS:
            validate_layout(spec["layout"])
            self.assertTrue(layout_slot_keys(spec["layout"]))

    @override_settings(ALLOWED_HOSTS=["*"])
    def test_point_detail_links_are_urlencoded(self):
        point = make_trade_point("A&B 1/2")
        self.client.force_login(make_admin())
        data = self.client.get(reverse("market_map:point", args=[point.pk])).json()
        self.assertTrue(data["links"]["charges"].endswith("?q=A%26B+1%2F2"))
        self.assertTrue(data["links"]["readings"].endswith("?q=A%26B+1%2F2"))

    def test_market_map_permission_by_role(self):
        from django.contrib.auth.models import Permission

        perm = "trade_points.view_market_map"
        self.assertTrue(make_admin().has_perm(perm))
        self.assertTrue(make_superuser().has_perm(perm))
        controller = make_controller(is_staff=True)
        self.assertFalse(controller.has_perm(perm))
        self.assertFalse(make_tenant().has_perm(perm))
        # Право можно выдать и вручную (группой / пользователю) — тогда карта открывается.
        controller.user_permissions.add(Permission.objects.get(codename="view_market_map"))
        controller = type(controller).objects.get(pk=controller.pk)
        self.assertTrue(controller.has_perm(perm))
        self.client.force_login(controller)
        with self.settings(ALLOWED_HOSTS=["*"]):
            self.assertEqual(self.client.get(DATA_URL).status_code, 200)

    @override_settings(ALLOWED_HOSTS=["*"])
    def test_top_menu_link_hidden_for_controllers(self):
        link = f'href="{PAGE_URL}"'
        self.client.force_login(make_admin())
        self.assertContains(self.client.get("/admin/"), link)
        self.client.force_login(make_controller(is_staff=True))
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, link)
