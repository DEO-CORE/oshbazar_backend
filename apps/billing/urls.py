"""URL routes for billing: periods, tariffs, charges, debt, consumption."""
from django.urls import path

from .views import (
    ChargeDetailView,
    ChargeListCreateView,
    ChargeRecalculateView,
    ChargeVoidView,
    ConsumptionView,
    CurrentPeriodView,
    DebtView,
    LedgerView,
    PeriodCloseView,
    PeriodListCreateView,
    TariffDetailView,
    TariffListCreateView,
)

app_name = "billing"

urlpatterns = [
    path("billing/periods/", PeriodListCreateView.as_view(), name="period-list"),
    path("billing/periods/current/", CurrentPeriodView.as_view(), name="period-current"),
    path("billing/periods/<uuid:pk>/close/", PeriodCloseView.as_view(), name="period-close"),
    path("billing/tariffs/", TariffListCreateView.as_view(), name="tariff-list"),
    path("billing/tariffs/<uuid:pk>/", TariffDetailView.as_view(), name="tariff-detail"),
    path("billing/charges/", ChargeListCreateView.as_view(), name="charge-list"),
    path("billing/charges/<uuid:pk>/", ChargeDetailView.as_view(), name="charge-detail"),
    path("billing/charges/<uuid:pk>/void/", ChargeVoidView.as_view(), name="charge-void"),
    path(
        "billing/charges/<uuid:pk>/recalculate/",
        ChargeRecalculateView.as_view(),
        name="charge-recalculate",
    ),
    path("billing/debt/", DebtView.as_view(), name="debt"),
    path("billing/ledger/", LedgerView.as_view(), name="ledger"),
    path("consumption/", ConsumptionView.as_view(), name="consumption"),
]
