"""Payment endpoints: initiate, status, webhooks, receipts."""
import json

from django.conf import settings
from django.shortcuts import render
from django.utils.translation import gettext as _
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.core.audit import log_action
from apps.core.permissions import IsAdminRole
from apps.core.texts import RECEIPT_TEXTS

from .models import Payment, PaymentStatus, Receipt
from .providers.registry import available_providers, default_provider_code
from .serializers import (
    PaymentInitiateSerializer,
    PaymentSerializer,
    ReceiptSerializer,
)
from .services import PaymentError, initiate_payment, process_webhook, refund_payment_record


def visible_payments(user):
    qs = Payment.objects.select_related("user", "trade_point").prefetch_related("allocations")
    if getattr(user, "is_staff_role", False):
        return qs
    return qs.filter(user=user)


@extend_schema(
    tags=["payments"],
    summary="Инициировать оплату через банк",
    request=PaymentInitiateSerializer,
    responses={201: PaymentSerializer},
    examples=[
        OpenApiExample(
            "Оплата конкретных начислений",
            value={"provider": "mock", "charge_ids": ["<uuid>"], "description": "Оплата за октябрь"},
            request_only=True,
        )
    ],
)
class PaymentInitiateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PaymentInitiateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            payment = initiate_payment(
                user=request.user,
                amount=data["amount"],
                provider_code=data.get("provider") or default_provider_code(),
                trade_point=data.get("trade_point_obj"),
                charge_ids=[str(c.id) for c in data.get("charges", [])],
                description=data.get("description", ""),
                idempotency_key=data.get("idempotency_key") or None,
                request=request,
            )
        except PaymentError as exc:
            return Response(
                {"errors": [{"code": exc.code, "message": exc.message, "field": None}]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        log_action(user=request.user, action="payments.initiated", target=payment, request=request,
                   extra={"amount": str(payment.amount), "provider": payment.provider})
        return Response(
            PaymentSerializer(payment, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["payments"], summary="История платежей")
class PaymentListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PaymentSerializer
    filterset_fields = ["status", "provider", "trade_point"]
    ordering_fields = ["created_at", "amount", "completed_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        return visible_payments(self.request.user)


@extend_schema(tags=["payments"], summary="Платёж")
class PaymentDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PaymentSerializer

    def get_object(self):
        payment = visible_payments(self.request.user).filter(pk=self.kwargs["pk"]).first()
        if payment is None:
            raise NotFound(_("Платёж не найден."))
        return payment


@extend_schema(tags=["payments"], summary="Состояние платежа (для опроса после оплаты)")
class PaymentStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        payment = visible_payments(request.user).filter(pk=pk).first()
        if payment is None:
            raise NotFound(_("Платёж не найден."))
        return Response(
            {
                "id": str(payment.id),
                "status": payment.status,
                "status_display": payment.get_status_display(),
                "amount": str(payment.amount),
                "currency": payment.currency,
                "provider": payment.provider,
                "provider_payment_id": payment.provider_payment_id,
                "redirect_url": payment.redirect_url,
                "completed_at": payment.completed_at,
                "receipt_id": str(payment.receipt.id) if hasattr(payment, "receipt") else None,
            }
        )


class WebhookThrottle(AnonRateThrottle):
    scope = "anon"


@extend_schema(
    tags=["payments"],
    summary="Вебхук банка",
    description=(
        "Уведомление банка о результате платежа. Проверяется HMAC-SHA256 подпись "
        "из заголовка `X-Signature` (секрет — PAYMENT_WEBHOOK_SECRET). Обработка идемпотентна."
    ),
    auth=[],
    responses={200: None, 400: None, 404: None},
    examples=[
        OpenApiExample(
            "Успешный платёж",
            value={"provider_payment_id": "BANK-123", "status": "success", "amount": 180.00},
            request_only=True,
        )
    ],
)
class PaymentWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [WebhookThrottle]

    def post(self, request, provider):
        if provider not in available_providers():
            return Response(
                {
                    "errors": [
                        {"code": "unknown_provider", "message": _("Неизвестный платёжный провайдер."),
                         "field": None}
                    ]
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            raw_body = request.body
            try:
                payload = json.loads(raw_body) if raw_body else {}
            except ValueError:
                payload = {}
            result = process_webhook(provider, payload, request.headers, raw_body)
        except PaymentError as exc:
            http_status = status.HTTP_404_NOT_FOUND if exc.code == "payment_not_found" else status.HTTP_400_BAD_REQUEST
            return Response(
                {"errors": [{"code": exc.code, "message": exc.message, "field": None}]},
                status=http_status,
            )
        return Response(result)


@extend_schema(
    tags=["payments"],
    summary="Песочница оплаты",
    description="Тестовый «банк»: подтверждает платёж без реальных денег (только в режиме песочницы).",
)
class MockCheckoutView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, provider_payment_id):
        payment = Payment.objects.filter(provider_payment_id=provider_payment_id).first()
        if payment is None:
            return Response(
                {"errors": [{"code": "not_found", "message": _("Платёж не найден."), "field": None}]},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {
                "sandbox": True,
                "message": "Тестовый банк. В реальной интеграции здесь интерфейс оплаты банка.",
                "payment_id": str(payment.id),
                "status": payment.status,
                "amount": str(payment.amount),
                "currency": payment.currency,
            }
        )


@extend_schema(tags=["payments"], summary="Возврат платежа (админ)")
class PaymentRefundView(APIView):
    permission_classes = [IsAdminRole]

    def post(self, request, pk):
        payment = Payment.objects.filter(pk=pk).first()
        if payment is None:
            raise NotFound(_("Платёж не найден."))
        if payment.status != PaymentStatus.SUCCEEDED:
            return Response(
                {
                    "errors": [
                        {"code": "not_refundable", "message": _("Возврат возможен только для успешного платежа."),
                         "field": None}
                    ]
                },
                status=status.HTTP_409_CONFLICT,
            )
        result = refund_payment_record(payment, reason=str(request.data.get("reason", "")))
        log_action(user=request.user, action="payments.refunded", target=payment, request=request)
        payment.refresh_from_db()
        return Response({"payment": PaymentSerializer(payment).data, "balance": result["balance"]})


@extend_schema(tags=["payments"], summary="Платёжные провайдеры")
class PaymentProvidersView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            {
                "default": default_provider_code(),
                "providers": [{"code": code, "class": dotted} for code, dotted in available_providers().items()],
                "currency": settings.PAYMENT_CURRENCY,
            }
        )


# ---------------------------------------------------------------------------
# Квитанции
# ---------------------------------------------------------------------------
def visible_receipts(user):
    qs = Receipt.objects.select_related("payment", "payment__user", "payment__trade_point")
    if getattr(user, "is_staff_role", False):
        return qs
    return qs.filter(payment__user=user)


@extend_schema(tags=["receipts"], summary="Электронные квитанции")
class ReceiptListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ReceiptSerializer
    ordering = ["-issued_at"]

    def get_queryset(self):
        return visible_receipts(self.request.user)


@extend_schema(tags=["receipts"], summary="Квитанция")
class ReceiptDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ReceiptSerializer

    def get_object(self):
        receipt = visible_receipts(self.request.user).filter(pk=self.kwargs["pk"]).first()
        if receipt is None:
            raise NotFound(_("Квитанция не найдена."))
        return receipt


@extend_schema(
    tags=["receipts"],
    summary="Печатная форма квитанции (HTML)",
    description="HTML-версия квитанции для просмотра/печати в мобильном приложении.",
    responses={(200, "text/html"): str},
)
class ReceiptPrintView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        receipt = visible_receipts(request.user).filter(pk=pk).first()
        if receipt is None:
            raise NotFound(_("Квитанция не найдена."))
        lang = request.query_params.get("lang", getattr(request.user, "language", "ru"))
        if lang not in RECEIPT_TEXTS:
            lang = "ru"
        context = {
            "receipt": receipt,
            "data": receipt.data,
            "texts": RECEIPT_TEXTS[lang],
            "lang": lang,
            "payment": receipt.payment,
        }
        return render(request, "receipts/receipt.html", context)
