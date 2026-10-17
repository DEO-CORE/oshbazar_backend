"""URL routes for reports and exports."""
from django.urls import path

from .views import (
    ConsumptionExportView,
    DashboardView,
    DebtExportView,
    PaymentsExportView,
)

app_name = "reports"

urlpatterns = [
    path("reports/dashboard/", DashboardView.as_view(), name="dashboard"),
    path("reports/export/consumption/", ConsumptionExportView.as_view(), name="export-consumption"),
    path("reports/export/debts/", DebtExportView.as_view(), name="export-debts"),
    path("reports/export/payments/", PaymentsExportView.as_view(), name="export-payments"),
]
