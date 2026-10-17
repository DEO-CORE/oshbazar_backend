"""URL routes for trade points, meters and QR codes."""
from django.urls import path

from .views import (
    TradePointDetailView,
    TradePointListCreateView,
    TradePointQRView,
    TradePointScanView,
    TradePointSummaryView,
)

app_name = "trade-points"

urlpatterns = [
    path("trade-points/", TradePointListCreateView.as_view(), name="list"),
    path("trade-points/scan/<str:code>/", TradePointScanView.as_view(), name="scan"),
    path("trade-points/<uuid:pk>/", TradePointDetailView.as_view(), name="detail"),
    path("trade-points/<uuid:pk>/qr/", TradePointQRView.as_view(), name="qr"),
    path("trade-points/<uuid:pk>/summary/", TradePointSummaryView.as_view(), name="summary"),
]
