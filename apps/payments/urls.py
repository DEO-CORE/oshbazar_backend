"""URL routes for payments and receipts."""
from django.urls import path

from .views import (
    MockCheckoutView,
    PaymentDetailView,
    PaymentInitiateView,
    PaymentListView,
    PaymentProvidersView,
    PaymentRefundView,
    PaymentStatusView,
    PaymentWebhookView,
    ReceiptDetailView,
    ReceiptListView,
    ReceiptPrintView,
)

app_name = "payments"

urlpatterns = [
    path("payments/initiate/", PaymentInitiateView.as_view(), name="initiate"),
    path("payments/providers/", PaymentProvidersView.as_view(), name="providers"),
    path("payments/webhook/<slug:provider>/", PaymentWebhookView.as_view(), name="webhook"),
    path("payments/mock/checkout/<str:provider_payment_id>/", MockCheckoutView.as_view(), name="mock-checkout"),
    path("payments/", PaymentListView.as_view(), name="list"),
    path("payments/<uuid:pk>/", PaymentDetailView.as_view(), name="detail"),
    path("payments/<uuid:pk>/status/", PaymentStatusView.as_view(), name="status"),
    path("payments/<uuid:pk>/refund/", PaymentRefundView.as_view(), name="refund"),
    path("receipts/", ReceiptListView.as_view(), name="receipt-list"),
    path("receipts/<uuid:pk>/", ReceiptDetailView.as_view(), name="receipt-detail"),
    path("receipts/<uuid:pk>/print/", ReceiptPrintView.as_view(), name="receipt-print"),
]
