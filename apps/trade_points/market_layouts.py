"""Исходные (стилизованные) схемы трёх соседних рынков.

Схемы нарисованы по фотографиям бумажных планов: пропорции и расположение
зданий, рядов и парковок переданы приблизительно, 1 единица ≈ 1 м.
Формат `layout` описан в `apps/trade_points/market_map.py`.

Модуль не зависит от моделей, поэтому его безопасно использовать
и в data-миграции, и в команде `seed_markets`.
"""
from __future__ import annotations

import math


def _r(value: float) -> float:
    return round(value, 2)


def osh_bazary() -> dict:
    """«Ош Базары»: длинный узкий участок, три полосы рядов, парковка на востоке."""
    band = {
        "rows": 4,
        "cols": 80,
        "slot": [2.6, 3.0],
        "gap": [0.3, 0.3],
        "aisle_every": 2,
        "aisle": 2.4,
        "order": "columns",
        "tone": "neutral",
    }
    blocks = []
    for index, (block_id, y) in enumerate((("А", 6), ("Б", 22), ("В", 38)), start=1):
        blocks.append(
            {
                **band,
                "id": block_id,
                "name": f"Ряд {block_id}",
                "aliases": [block_id, f"Ряд {block_id}", f"Корпус {index}"],
                "x": 24,
                "y": y,
            }
        )
    return {
        "version": 1,
        "offset": [258, 0],
        "outline": [[0, 3], [456, 0], [440, 54], [14, 54]],
        "label": {"text": "Ош Базары", "at": [190, -8]},
        "roads": [
            {"points": [[-22, 70], [470, 70]], "width": 12},
            {"points": [[-22, -14], [470, -16]], "width": 10},
        ],
        "zones": [
            {"kind": "building", "name": "Администрация", "rect": [360, 6, 32, 18]},
            {"kind": "service", "name": "ТП", "rect": [360, 29, 14, 10]},
            {"kind": "building", "name": "Склад", "rect": [378, 29, 14, 20]},
            {
                "kind": "parking",
                "name": "Парковка",
                "places": 293,
                "polygon": [[400, 5], [452, 1], [439, 51], [400, 51]],
            },
        ],
        "labels": [],
        "blocks": blocks,
    }


def bekere() -> dict:
    """«Бекере Базары»: главный корпус и малые здания на СЗ, ряды на востоке и юге."""
    red = {
        "rows": 6,
        "cols": 28,
        "slot": [2.6, 2.8],
        "gap": [0.3, 0.3],
        "aisle_every": 2,
        "aisle": 2.2,
        "order": "columns",
        "tone": "red",
    }
    blocks = [
        {**red, "id": f"К{i}", "name": f"Ряды К{i}", "aliases": [f"К{i}", f"Ряд К{i}"], "x": 142, "y": y}
        for i, y in ((1, 8), (2, 32), (3, 56))
    ]
    blocks.append(
        {
            "id": "Ю",
            "name": "Южный ряд",
            "aliases": ["Ю", "Южный ряд"],
            "x": 10,
            "y": 127,
            "rows": 4,
            "cols": 70,
            "slot": [2.8, 3.4],
            "gap": [0.3, 0.3],
            "aisle_every": 10,
            "aisle": 2.0,
            "row_aisle_every": 2,
            "row_aisle": 3.0,
            "order": "rows",
            "tone": "blue",
        }
    )
    return {
        "version": 1,
        "offset": [258, 86],
        "outline": [[0, 0], [262, 0], [262, 150], [0, 156]],
        "label": {"text": "Бекере Базары", "at": [131, 172]},
        "roads": [],
        "zones": [
            {"kind": "building", "name": "Главный корпус", "rect": [8, 8, 92, 44], "tone": "warm", "levels": 2},
            {"kind": "building", "name": "Корпус 2", "rect": [106, 8, 26, 14], "tone": "warm"},
            {"kind": "building", "name": "Корпус 3", "rect": [106, 27, 26, 14], "tone": "warm"},
            {"kind": "building", "name": "", "rect": [8, 58, 28, 16], "tone": "warm"},
            {"kind": "building", "name": "", "rect": [41, 58, 28, 16], "tone": "warm"},
            {"kind": "building", "name": "", "rect": [74, 58, 26, 16], "tone": "warm"},
            {"kind": "parking", "name": "Парковка", "places": 800, "rect": [8, 82, 126, 38]},
            {"kind": "service", "name": "Склады", "rect": [142, 84, 112, 32]},
        ],
        "labels": [],
        "blocks": blocks,
    }


def zhibek_zholu() -> dict:
    """Третий рынок: высокая трапеция, здания вдоль северной дороги, павильоны, парковка."""
    p0 = (20.0, 46.0)
    angle = math.degrees(math.atan2(-40, 222))  # северная граница участка
    ux, uy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    nx, ny = -uy, ux  # нормаль внутрь участка
    zones = []
    for i in range(6):
        along, depth, width = i * 35 + 15, 7, 31
        cx, cy = p0[0] + ux * along + nx * depth, p0[1] + uy * along + ny * depth
        zones.append(
            {
                "kind": "building",
                "name": "",
                "rect": [_r(cx - width / 2), _r(cy - 6), width, 12],
                "rotate": _r(angle),
            }
        )
    zones += [
        {
            "kind": "parking",
            "name": "Парковка",
            "places": 0,
            "polygon": [[17, 124], [64, 124], [64, 279], [22, 281]],
        },
        {"kind": "hall", "name": "Павильон 1", "rect": [72, 126, 46, 50], "levels": 2},
        {"kind": "hall", "name": "Павильон 2", "rect": [124, 126, 46, 50], "levels": 2},
        {"kind": "hall", "name": "Павильон 3", "rect": [176, 126, 52, 50], "levels": 2},
    ]
    top_x, top_y = p0[0] + nx * 18, p0[1] + ny * 18
    lower = {
        "rows": 6,
        "slot": [3.0, 3.0],
        "gap": [0.3, 0.3],
        "row_aisle_every": 2,
        "row_aisle": 3.0,
        "order": "rows",
        "tone": "neutral",
    }
    blocks = [
        {
            "id": "Н",
            "name": "Северные ряды",
            "aliases": ["Н", "Северные ряды"],
            "x": _r(top_x),
            "y": _r(top_y),
            "rotate": _r(angle),
            "rows": 14,
            "cols": 20,
            "slot": [3.0, 3.2],
            "gap": [0.3, 0.3],
            "aisle_every": 2,
            "aisle": 11.0,
            "order": "columns",
            "tone": "neutral",
        },
        {**lower, "id": "Д1", "name": "Блок Д1", "aliases": ["Д1"], "x": 72, "y": 188, "cols": 22},
        {**lower, "id": "Д2", "name": "Блок Д2", "aliases": ["Д2"], "x": 154, "y": 188, "cols": 22},
        {**lower, "id": "Д3", "name": "Блок Д3", "aliases": ["Д3"], "x": 72, "y": 222, "cols": 46},
    ]
    return {
        "version": 1,
        "offset": [0, 0],
        "outline": [[10, 40], [232, 0], [240, 282], [22, 288]],
        "label": {"text": "Жибек Жолу Базары", "at": [128, 304]},
        "roads": [
            {"points": [[-12, 30], [250, -16]], "width": 10},
            {"points": [[249, -24], [249, 300]], "width": 12},
        ],
        "zones": zones,
        "labels": [],
        "blocks": blocks,
    }


DEFAULT_MARKETS = [
    {
        "slug": "osh-bazary",
        "name": "Ош Базары",
        "short_code": "ОБ",
        "color": "#2563EB",
        "area_m2": "22680",
        "parking_places": 293,
        "sort_order": 1,
        "description": "Длинный узкий участок: три полосы торговых рядов, парковка на 293 места.",
        "layout": osh_bazary,
    },
    {
        "slug": "bekere",
        "name": "Бекере Базары",
        "short_code": "ББ",
        "color": "#C2410C",
        "area_m2": "39868",
        "parking_places": 800,
        "sort_order": 2,
        "description": "Главный корпус и малые здания на северо-западе, ряды на востоке и юге, парковка на 800 мест.",
        "layout": bekere,
    },
    {
        "slug": "zhibek-zholu",
        "name": "Жибек Жолу Базары",
        "short_code": "ЖЖ",
        "color": "#0F766E",
        "area_m2": "59000",
        "parking_places": 0,
        "sort_order": 3,
        "description": "Здания вдоль северной дороги, крытые павильоны, большая парковка на западе.",
        "layout": zhibek_zholu,
    },
]
