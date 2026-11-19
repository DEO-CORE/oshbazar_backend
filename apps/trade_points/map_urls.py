"""Маршруты карты рынков: подключаются в config/urls.py как admin/market-map/."""
from django.urls import path

from .map_views import map_data, map_page, map_point

app_name = "market_map"

urlpatterns = [
    path("", map_page, name="index"),
    path("data/", map_data, name="data"),
    path("points/<uuid:pk>/", map_point, name="point"),
]
