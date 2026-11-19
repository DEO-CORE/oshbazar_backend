"""Карта рынков: геометрия схем, статусы мест и автоматическая расстановка точек.

Формат `Market.layout` (JSON, 1 единица ≈ 1 м, ось Y направлена вниз)
====================================================================

.. code-block:: json

    {
      "version": 1,
      "offset": [258, 0],              // положение рынка на общей схеме площадки
      "outline": [[0, 3], [456, 0], [440, 54], [14, 54]],   // граница участка
      "label": {"text": "Ош Базары", "at": [190, -8]},       // подпись рынка
      "roads": [{"points": [[-22, 70], [470, 70]], "width": 12}],
      "zones": [                        // здания, павильоны, парковки, газоны
        {"kind": "building", "name": "Администрация", "rect": [360, 6, 32, 18],
         "rotate": 0, "tone": "warm", "levels": 2},
        {"kind": "parking", "name": "Парковка", "places": 293,
         "polygon": [[400, 5], [452, 1], [439, 51], [400, 51]]}
      ],
      "labels": [{"text": "Вход", "at": [10, 20], "size": 6}],
      "blocks": [                       // блоки торговых мест (сетка ячеек)
        {"id": "А", "name": "Ряд А", "aliases": ["А", "Корпус 1"],
         "x": 24, "y": 6, "rotate": 0,
         "cols": 80, "rows": 4, "slot": [2.6, 3.0], "gap": [0.3, 0.3],
         "aisle_every": 2, "aisle": 2.4, "row_aisle_every": 0, "row_aisle": 0,
         "order": "columns", "tone": "neutral"}
      ]
    }

* Все координаты внутри рынка — локальные, относительно `offset`.
* `zones[].kind`: building | hall | parking | service | green | plaza.
  Прямоугольник задаётся `rect: [x, y, w, h]` (+ `rotate` вокруг центра)
  либо произвольный многоугольник `polygon`.
* Блок — сетка `cols × rows` ячеек размером `slot` с зазором `gap`.
  `aisle_every`/`aisle` — дополнительный проход после каждых N столбцов,
  `row_aisle_every`/`row_aisle` — то же для строк. `rotate` — поворот
  блока в градусах вокруг его угла (`x`, `y`).
* Ячейки нумеруются с 1: `order = "columns"` — по вертикальным рядам
  (сверху вниз, затем следующий ряд), `"rows"` — построчно слева направо.
  Ключ ячейки — `"<id блока>-<номер>"`, например «А-17»; он хранится
  в `TradePoint.map_slot`.
* `aliases` — значения поля `TradePoint.block`, которые относятся к блоку
  (используется при автоматической расстановке по корпусу/номеру места).
"""
from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, F, OuterRef, Prefetch, Subquery, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

# Nil UUID: URL template id, replaced with the real point id in map.js.
URL_TEMPLATE_ID = str(uuid.UUID(int=0))

ZERO = Decimal("0")

STATUS_PAID = "paid"
STATUS_DEBT = "debt"
STATUS_MISSING = "missing"
STATUS_PENDING = "pending"
STATUS_INACTIVE = "inactive"
STATUS_FREE = "free"

STATUS_LABELS = {
    STATUS_PAID: "Оплачено",
    STATUS_DEBT: "Есть долг",
    STATUS_MISSING: "Нет показаний",
    STATUS_PENDING: "На проверке",
    STATUS_INACTIVE: "Неактивна",
    STATUS_FREE: "Свободно",
}

ZONE_KINDS = {"building", "hall", "parking", "service", "green", "plaza"}


# ---------------------------------------------------------------------------
# Геометрия
# ---------------------------------------------------------------------------
def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _pair(value, default) -> tuple[float, float]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return _num(value[0]), _num(value[1])
    return default


@dataclass
class BlockGrid:
    """Сетка ячеек блока в его локальных координатах."""

    id: str
    cols: int
    rows: int
    slot_w: float
    slot_h: float
    gap_x: float
    gap_y: float
    aisle_every: int
    aisle: float
    row_aisle_every: int
    row_aisle: float
    order: str
    raw: dict = field(repr=False, default_factory=dict)

    @classmethod
    def from_layout(cls, block: dict) -> "BlockGrid":
        slot_w, slot_h = _pair(block.get("slot"), (3.0, 3.0))
        gap_x, gap_y = _pair(block.get("gap"), (0.3, 0.3))
        return cls(
            id=str(block.get("id", "")).strip(),
            cols=max(int(_num(block.get("cols"), 1)), 0),
            rows=max(int(_num(block.get("rows"), 1)), 0),
            slot_w=slot_w,
            slot_h=slot_h,
            gap_x=gap_x,
            gap_y=gap_y,
            aisle_every=int(_num(block.get("aisle_every"), 0)),
            aisle=_num(block.get("aisle"), 0),
            row_aisle_every=int(_num(block.get("row_aisle_every"), 0)),
            row_aisle=_num(block.get("row_aisle"), 0),
            order="rows" if block.get("order") == "rows" else "columns",
            raw=block,
        )

    def col_x(self, col: int) -> float:
        extra = (col // self.aisle_every) * self.aisle if self.aisle_every > 0 else 0
        return col * (self.slot_w + self.gap_x) + extra

    def row_y(self, row: int) -> float:
        extra = (row // self.row_aisle_every) * self.row_aisle if self.row_aisle_every > 0 else 0
        return row * (self.slot_h + self.gap_y) + extra

    @property
    def width(self) -> float:
        return self.col_x(self.cols - 1) + self.slot_w if self.cols else 0

    @property
    def height(self) -> float:
        return self.row_y(self.rows - 1) + self.slot_h if self.rows else 0

    @property
    def capacity(self) -> int:
        return self.cols * self.rows

    def number(self, col: int, row: int) -> int:
        if self.order == "rows":
            return row * self.cols + col + 1
        return col * self.rows + row + 1

    def slots(self):
        """(номер, x, y) каждой ячейки в порядке нумерации."""
        cells = []
        for col in range(self.cols):
            for row in range(self.rows):
                cells.append((self.number(col, row), round(self.col_x(col), 2), round(self.row_y(row), 2)))
        cells.sort(key=lambda cell: cell[0])
        return cells

    def key(self, number: int) -> str:
        return f"{self.id}-{number}"


def slot_key(block_id: str, number: int) -> str:
    return f"{block_id}-{number}"


def layout_blocks(layout: dict | None) -> list[BlockGrid]:
    blocks = (layout or {}).get("blocks") or []
    return [BlockGrid.from_layout(block) for block in blocks if isinstance(block, dict)]


def layout_slot_keys(layout: dict | None) -> list[str]:
    keys = []
    for grid in layout_blocks(layout):
        keys.extend(grid.key(number) for number, _x, _y in grid.slots())
    return keys


def slot_center(layout: dict, key: str) -> tuple[float, float] | None:
    """Центр ячейки в координатах общей схемы площадки."""
    block_id, _, number = key.rpartition("-")
    ox, oy = _pair(layout.get("offset"), (0.0, 0.0))
    for grid in layout_blocks(layout):
        if grid.id != block_id:
            continue
        for n, lx, ly in grid.slots():
            if str(n) == number:
                angle = math.radians(_num(grid.raw.get("rotate"), 0))
                px, py = lx + grid.slot_w / 2, ly + grid.slot_h / 2
                bx, by = _num(grid.raw.get("x")), _num(grid.raw.get("y"))
                return (
                    round(ox + bx + px * math.cos(angle) - py * math.sin(angle), 2),
                    round(oy + by + px * math.sin(angle) + py * math.cos(angle), 2),
                )
    return None


def layout_bounds(layouts: list[dict]) -> list[float]:
    """[min_x, min_y, max_x, max_y] по границам участков и дорогам."""
    xs, ys = [], []
    for layout in layouts:
        ox, oy = _pair(layout.get("offset"), (0.0, 0.0))
        points = list(layout.get("outline") or [])
        for road in layout.get("roads") or []:
            points.extend(road.get("points") or [])
        for point in points:
            x, y = _pair(point, (None, None))
            if x is None:
                continue
            xs.append(ox + x)
            ys.append(oy + y)
    if not xs:
        return [0, 0, 100, 100]
    return [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)]


def validate_layout(layout) -> None:
    """Проверка схемы перед сохранением рынка в админке."""
    if layout in (None, {}, ""):
        return
    if not isinstance(layout, dict):
        raise ValidationError("Схема должна быть JSON-объектом.")
    errors = []
    outline = layout.get("outline", [])
    if outline and (not isinstance(outline, list) or len(outline) < 3):
        errors.append("outline: нужно минимум 3 точки [x, y].")
    for index, zone in enumerate(layout.get("zones") or []):
        if not isinstance(zone, dict):
            errors.append(f"zones[{index}]: ожидается объект.")
            continue
        if zone.get("kind") not in ZONE_KINDS:
            errors.append(f"zones[{index}].kind: одно из {', '.join(sorted(ZONE_KINDS))}.")
        if "rect" not in zone and "polygon" not in zone:
            errors.append(f"zones[{index}]: укажите rect [x, y, w, h] или polygon.")
    seen: set[str] = set()
    for index, block in enumerate(layout.get("blocks") or []):
        if not isinstance(block, dict):
            errors.append(f"blocks[{index}]: ожидается объект.")
            continue
        block_id = str(block.get("id", "")).strip()
        if not block_id:
            errors.append(f"blocks[{index}].id: обязательное поле.")
        elif block_id in seen:
            errors.append(f"blocks[{index}].id: «{block_id}» повторяется.")
        seen.add(block_id)
        for key in ("cols", "rows"):
            value = block.get(key)
            if not isinstance(value, int) or value < 1 or value > 500:
                errors.append(f"blocks[{index}].{key}: целое число от 1 до 500.")
    if errors:
        raise ValidationError(errors)


# ---------------------------------------------------------------------------
# Статусы и данные для карты
# ---------------------------------------------------------------------------
def point_status(*, is_active: bool, debt: Decimal, reading_status: str | None) -> str:
    from apps.readings.models import ReadingStatus

    if not is_active:
        return STATUS_INACTIVE
    if debt > ZERO:
        return STATUS_DEBT
    if reading_status == ReadingStatus.PENDING:
        return STATUS_PENDING
    if reading_status is None:
        return STATUS_MISSING
    return STATUS_PAID


def debt_levels(debts: list[Decimal]) -> tuple[list[Decimal], callable]:
    """Пороги интенсивности долга (4 уровня по квартилям) и функция уровня."""
    positive = sorted(d for d in debts if d > ZERO)
    if not positive:
        return [], lambda _d: 0
    thresholds = [positive[min(len(positive) - 1, int(len(positive) * q))] for q in (0.25, 0.5, 0.75)]

    def level(value: Decimal) -> int:
        if value <= ZERO:
            return 0
        return 1 + sum(1 for t in thresholds if value > t)

    return thresholds, level


def map_points_queryset(period):
    from apps.billing.models import Charge, ChargeStatus
    from apps.readings.models import MeterReading, ReadingStatus

    from .models import Meter, TradePoint, TradePointAccess

    debt = (
        Charge.objects.filter(trade_point=OuterRef("pk"))
        .exclude(status=ChargeStatus.VOID)
        .order_by()
        .values("trade_point")
        .annotate(total=Sum(F("amount") - F("paid_amount")))
        .values("total")[:1]
    )
    reading = (
        MeterReading.objects.filter(trade_point=OuterRef("pk"), period=period)
        .exclude(status=ReadingStatus.REJECTED)
        .order_by("-submitted_at")
    )
    return (
        TradePoint.objects.select_related("market")
        .annotate(
            debt_total=Subquery(debt, output_field=DecimalField(max_digits=14, decimal_places=2)),
            period_reading_status=Subquery(reading.values("status")[:1]),
        )
        .prefetch_related(
            Prefetch(
                "user_accesses",
                queryset=TradePointAccess.objects.filter(is_active=True)
                .select_related("user")
                .order_by("created_at"),
                to_attr="active_accesses",
            ),
            Prefetch(
                "meters",
                queryset=Meter.objects.filter(is_active=True).order_by("-is_primary", "serial_number"),
                to_attr="active_meters",
            ),
        )
        .order_by("code")
    )


def _money(value) -> str:
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


def _market_payload(market) -> dict:
    layout = market.layout or {}
    blocks = []
    for grid in layout_blocks(layout):
        raw = grid.raw
        blocks.append(
            {
                "id": grid.id,
                "name": raw.get("name") or grid.id,
                "tone": raw.get("tone", "neutral"),
                "x": _num(raw.get("x")),
                "y": _num(raw.get("y")),
                "rotate": _num(raw.get("rotate")),
                "w": round(grid.width, 2),
                "h": round(grid.height, 2),
                "slot": [grid.slot_w, grid.slot_h],
                "slots": grid.slots(),
            }
        )
    return {
        "id": str(market.pk),
        "slug": market.slug,
        "name": market.name,
        "short_code": market.short_code,
        "color": market.color,
        "area_m2": float(market.area_m2) if market.area_m2 is not None else None,
        "parking_places": market.parking_places,
        "description": market.description,
        "admin_url": reverse("admin:trade_points_market_change", args=[market.pk]),
        "layout": {
            "offset": list(_pair(layout.get("offset"), (0.0, 0.0))),
            "outline": layout.get("outline") or [],
            "label": layout.get("label") or {"text": market.name, "at": [0, -8]},
            "roads": layout.get("roads") or [],
            "zones": layout.get("zones") or [],
            "labels": layout.get("labels") or [],
        },
        "blocks": blocks,
    }


def build_map_payload() -> dict:
    """Данные для карты: схемы рынков, точки со статусами и сводка (≈6 SQL-запросов)."""
    from django.conf import settings

    from apps.billing.services import current_period

    from .models import Market

    period = current_period()
    markets = list(Market.objects.filter(is_active=True).order_by("sort_order", "name"))
    market_payloads = [_market_payload(market) for market in markets]
    slots_by_market = {
        m["slug"]: {slot_key(b["id"], s[0]) for b in m["blocks"] for s in b["slots"]} for m in market_payloads
    }

    rows = list(map_points_queryset(period))
    thresholds, level = debt_levels([row.debt_total or ZERO for row in rows])

    points = []
    for row in rows:
        debt = max(row.debt_total or ZERO, ZERO)
        status = point_status(is_active=row.is_active, debt=debt, reading_status=row.period_reading_status)
        market_slug = row.market.slug if row.market_id else None
        placed = bool(market_slug and row.map_slot and row.map_slot in slots_by_market.get(market_slug, ()))
        meter = row.active_meters[0] if row.active_meters else None
        points.append(
            {
                "id": str(row.pk),
                "code": row.code,
                "name": row.name,
                "block": row.block,
                "place": row.place_number,
                "market": market_slug,
                "slot": row.map_slot if placed else None,
                "status": status,
                "debt": _money(debt),
                "debt_level": level(debt),
                "reading": row.period_reading_status,
                "meter": meter.serial_number if meter else None,
                "tenants": [
                    {
                        "name": access.user.full_name or access.user.phone,
                        "phone": access.user.phone,
                        "role": access.access_role,
                    }
                    for access in row.active_accesses
                ],
            }
        )

    summary = {}
    for market in market_payloads + [{"slug": "all", "blocks": None}]:
        slug = market["slug"]
        scoped = [p for p in points if slug == "all" or p["market"] == slug]
        active = [p for p in scoped if p["status"] != STATUS_INACTIVE]
        submitted = [p for p in active if p["reading"] is not None]
        total_slots = (
            sum(len(v) for v in slots_by_market.values()) if slug == "all" else len(slots_by_market[slug])
        )
        placed = sum(1 for p in scoped if p["slot"])
        counts = {key: 0 for key in STATUS_LABELS}
        for p in scoped:
            if p["slot"]:
                counts[p["status"]] += 1
        counts[STATUS_FREE] = max(total_slots - placed, 0)
        summary[slug] = {
            "points": len(scoped),
            "active": len(active),
            "placed": placed,
            "slots": total_slots,
            "occupancy": round(placed * 100 / total_slots, 1) if total_slots else 0,
            "debt_total": _money(sum((Decimal(p["debt"]) for p in scoped), ZERO)),
            "debtors": sum(1 for p in scoped if p["status"] == STATUS_DEBT),
            "readings_submitted": len(submitted),
            "readings_pct": round(len(submitted) * 100 / len(active), 1) if active else 0,
            "pending": sum(1 for p in scoped if p["status"] == STATUS_PENDING),
            "statuses": counts,
        }

    return {
        "generated_at": timezone.now().isoformat(),
        "currency": settings.PAYMENT_CURRENCY,
        "period": {"label": period.label, "title": period.title},
        "statuses": [{"key": key, "label": label} for key, label in STATUS_LABELS.items()],
        "debt_thresholds": [_money(t) for t in thresholds],
        "bounds": layout_bounds([m["layout"] for m in market_payloads]),
        "markets": market_payloads,
        "points": points,
        "unplaced": sum(1 for p in points if not p["slot"]),
        "summary": summary,
        "urls": {
            "point_detail": reverse("market_map:point", args=[URL_TEMPLATE_ID]),
            "tradepoint_change": reverse(
                "admin:trade_points_tradepoint_change", args=[URL_TEMPLATE_ID]
            ),
        },
    }


def point_detail(point) -> dict:
    """Подробности для боковой панели: арендаторы, счётчики, показание, долг, аванс."""
    from apps.billing.models import Charge, ChargeStatus, LedgerEntry
    from apps.billing.services import current_period
    from apps.readings.models import MeterReading, ReadingStatus

    period = current_period()
    accesses = list(point.user_accesses.filter(is_active=True).select_related("user").order_by("created_at"))
    meters = list(point.meters.order_by("-is_active", "-is_primary", "serial_number"))
    last = (
        MeterReading.objects.filter(trade_point=point)
        .exclude(status=ReadingStatus.REJECTED)
        .select_related("period", "meter")
        .order_by("-period__year", "-period__month", "-submitted_at")
        .first()
    )
    current = (
        MeterReading.objects.filter(trade_point=point, period=period)
        .exclude(status=ReadingStatus.REJECTED)
        .order_by("-submitted_at")
        .values_list("status", flat=True)
        .first()
    )
    charges = Charge.objects.filter(trade_point=point).exclude(status=ChargeStatus.VOID)
    agg = charges.aggregate(amount=Sum("amount"), paid=Sum("paid_amount"))
    debt = max((agg["amount"] or ZERO) - (agg["paid"] or ZERO), ZERO)
    unpaid = charges.filter(paid_amount__lt=F("amount")).count()
    current_charge = charges.filter(period=period).order_by("-created_at").first()

    payer = None
    for role in ("owner", "tenant", "employee"):
        payer = next((a.user for a in accesses if a.access_role == role), None)
        if payer:
            break
    credit = ZERO
    if payer is not None:
        balance = LedgerEntry.objects.filter(user=payer).aggregate(total=Sum("amount"))["total"] or ZERO
        credit = -balance if balance < ZERO else ZERO

    status = point_status(is_active=point.is_active, debt=debt, reading_status=current)
    role_labels = dict(point.user_accesses.model._meta.get_field("access_role").choices)
    q = "?" + urlencode({"q": point.code})
    return {
        "id": str(point.pk),
        "code": point.code,
        "name": point.name,
        "block": point.block,
        "place": point.place_number,
        "area_m2": str(point.area_m2) if point.area_m2 is not None else None,
        "market": point.market.name if point.market_id else None,
        "slot": point.map_slot or None,
        "is_active": point.is_active,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "period": {"label": period.label, "title": period.title},
        "debt": _money(debt),
        "credit": _money(credit),
        "unpaid_charges": unpaid,
        "current_charge": (
            {
                "amount": _money(current_charge.amount),
                "paid": _money(current_charge.paid_amount),
                "consumption": str(current_charge.consumption),
                "status": current_charge.get_status_display(),
                "due_date": current_charge.due_date.isoformat() if current_charge.due_date else None,
            }
            if current_charge
            else None
        ),
        "tenants": [
            {
                "name": a.user.full_name or a.user.phone,
                "phone": a.user.phone,
                "inn": a.user.inn,
                "role": role_labels.get(a.access_role, a.access_role),
                "admin_url": reverse("admin:users_user_change", args=[a.user_id]),
            }
            for a in accesses
        ],
        "meters": [
            {
                "serial": m.serial_number,
                "model": m.model_name,
                "is_primary": m.is_primary,
                "is_active": m.is_active,
                "admin_url": reverse("admin:trade_points_meter_change", args=[m.pk]),
            }
            for m in meters
        ],
        "last_reading": (
            {
                "value": str(last.value),
                "previous": str(last.previous_value),
                "consumption": str(last.consumption),
                "period": last.period.title,
                "status": last.status,
                "status_label": last.get_status_display(),
                "submitted_at": timezone.localtime(last.submitted_at).strftime("%d.%m.%Y %H:%M"),
                "admin_url": reverse("admin:readings_meterreading_change", args=[last.pk]),
            }
            if last
            else None
        ),
        "links": {
            "change": reverse("admin:trade_points_tradepoint_change", args=[point.pk]),
            "readings": reverse("admin:readings_meterreading_changelist") + q,
            "charges": reverse("admin:billing_charge_changelist") + q,
        },
    }


# ---------------------------------------------------------------------------
# Автоматическая расстановка точек по местам
# ---------------------------------------------------------------------------
def _norm(value: str) -> str:
    return re.sub(r"[\s\-_.]+", "", (value or "").strip().lower())


def _place_number(value: str) -> int | None:
    match = re.search(r"\d+", value or "")
    return int(match.group()) if match else None


@dataclass
class PlacementResult:
    placed: int = 0
    by_number: int = 0
    by_fill: int = 0
    skipped: int = 0
    reset: int = 0
    details: list[str] = field(default_factory=list)


@transaction.atomic
def auto_place(*, market_slugs=None, default_market=None, fill: bool = False, reset: bool = False,
               dry_run: bool = False, point_ids=None) -> PlacementResult:
    """Расставляет точки по ячейкам схем.

    1. Рынок точки: `TradePoint.market`, иначе рынок, в схеме которого есть
       блок с подходящим `aliases`/`id` для `TradePoint.block`, иначе `default_market`.
    2. Ячейка: блок по `TradePoint.block` + номер из `place_number` («12» → «А-12»).
    3. С `fill=True` — оставшиеся точки занимают первые свободные ячейки рынка.
    Уже расставленные точки не трогаются, если не указан `reset`.
    `point_ids` — расставлять только эти точки (места остальных считаются занятыми).
    """
    from .models import Market, TradePoint

    result = PlacementResult()
    markets = list(Market.objects.order_by("sort_order", "name"))
    if market_slugs:
        markets = [m for m in markets if m.slug in set(market_slugs)]
    by_slug = {m.slug: m for m in Market.objects.all()}
    if default_market is not None and not isinstance(default_market, Market):
        default_market = by_slug.get(default_market)

    grids = {m.pk: layout_blocks(m.layout) for m in markets}
    alias_index: dict[str, tuple] = {}
    for market in markets:
        for grid in grids[market.pk]:
            for alias in [grid.id, grid.raw.get("name", ""), *(grid.raw.get("aliases") or [])]:
                alias_index.setdefault(_norm(str(alias)), (market, grid))
    market_ids = {m.pk for m in markets}

    points = TradePoint.objects.select_related("market").order_by("block", "place_number", "code")
    if market_slugs:
        points = points.filter(market__in=markets) | points.filter(market__isnull=True)
    points = list(points)
    if point_ids is not None:
        wanted = {str(pk) for pk in point_ids}
        targets = [p for p in points if str(p.pk) in wanted]
    else:
        targets = points

    if reset:
        for point in targets:
            if point.map_slot and (point.market_id in market_ids):
                point.map_slot = ""
                result.reset += 1
        if not dry_run:
            TradePoint.objects.filter(pk__in=[p.pk for p in targets if p.market_id in market_ids]).update(map_slot="")

    valid = {m.pk: set(layout_slot_keys(m.layout)) for m in markets}
    taken = {m.pk: set() for m in markets}
    for point in points:
        if point.market_id in valid and point.map_slot:
            if point.map_slot in valid[point.market_id]:
                taken[point.market_id].add(point.map_slot)
            else:
                point.map_slot = ""

    def save(point, market, key, how):
        point.market = market
        point.map_slot = key
        taken[market.pk].add(key)
        result.placed += 1
        if how == "number":
            result.by_number += 1
        else:
            result.by_fill += 1
        if not dry_run:
            TradePoint.objects.filter(pk=point.pk).update(market=market, map_slot=key)

    leftovers = []
    for point in targets:
        if point.map_slot and point.market_id in valid:
            continue
        match = alias_index.get(_norm(point.block)) if point.block else None
        market = point.market if point.market_id else None
        if market is not None and market.pk not in market_ids:
            continue
        grid = None
        if match and (market is None or match[0].pk == market.pk):
            market, grid = match
        if market is None:
            market = default_market if default_market and default_market.pk in market_ids else None
        if market is None:
            result.skipped += 1
            continue
        number = _place_number(point.place_number)
        if grid is not None and number:
            key = grid.key(number)
            if key in valid[market.pk] and key not in taken[market.pk]:
                save(point, market, key, "number")
                continue
        leftovers.append((point, market))

    if fill:
        for point, market in leftovers:
            free = next((k for k in layout_slot_keys(market.layout) if k not in taken[market.pk]), None)
            if free is None:
                result.skipped += 1
                result.details.append(f"{point.code}: нет свободных мест на рынке «{market.name}»")
                continue
            save(point, market, free, "fill")
    else:
        for point, market in leftovers:
            result.skipped += 1
            if not point.market_id and not dry_run:
                TradePoint.objects.filter(pk=point.pk).update(market=market)
    return result


def seed_markets(*, reset_layout: bool = False) -> tuple[int, int]:
    """Создаёт рынки из `market_layouts.DEFAULT_MARKETS`. Возвращает (создано, обновлено схем)."""
    from .market_layouts import DEFAULT_MARKETS
    from .models import Market

    created = updated = 0
    for spec in DEFAULT_MARKETS:
        defaults = {
            "name": spec["name"],
            "short_code": spec["short_code"],
            "color": spec["color"],
            "area_m2": Decimal(spec["area_m2"]),
            "parking_places": spec["parking_places"],
            "sort_order": spec["sort_order"],
            "description": spec["description"],
            "layout": spec["layout"](),
        }
        market, is_new = Market.objects.get_or_create(slug=spec["slug"], defaults=defaults)
        if is_new:
            created += 1
        elif reset_layout or not market.layout:
            market.layout = defaults["layout"]
            market.save(update_fields=["layout", "updated_at"])
            updated += 1
    return created, updated
