"""Staff dashboard and CSV export endpoints (ТЗ, п. 25.25–25.26)."""
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext as _
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import BillingPeriod, Charge, ChargeStatus
from apps.billing.services import current_period, money
from apps.core.permissions import IsStaffRole
from apps.notifications.models import Notification
from apps.payments.models import Payment, PaymentStatus
from apps.readings.models import MeterReading, ReadingStatus
from apps.trade_points.models import Meter, TradePoint
from apps.users.models import UserRole

from .csv import csv_response

User = get_user_model()


@extend_schema(
    tags=["reports"],
    summary="Сводка для администрации",
    description="Ключевые показатели: точки, арендаторы, начисления, сборы и задолженность за период.",
)
class DashboardView(APIView):
    permission_classes = [IsStaffRole]

    def get(self, request):
        period = current_period()
        charges = Charge.objects.filter(period=period).exclude(status=ChargeStatus.VOID)
        aggregates = charges.aggregate(
            charged=Sum("amount"),
            paid=Sum("paid_amount"),
            count=Count("id"),
        )
        charged = aggregates["charged"] or Decimal("0")
        paid = aggregates["paid"] or Decimal("0")

        payments = Payment.objects.filter(created_at__date=timezone.localdate())
        today_collected = payments.filter(status=PaymentStatus.SUCCEEDED).aggregate(
            total=Sum("amount")
        )["total"] or Decimal("0")

        readings = MeterReading.objects.filter(period=period)
        reading_stats = readings.aggregate(
            total=Count("id"),
            pending=Count("id", filter=Q(status=ReadingStatus.PENDING)),
            approved=Count("id", filter=Q(status=ReadingStatus.APPROVED)),
            rejected=Count("id", filter=Q(status=ReadingStatus.REJECTED)),
        )

        total_balance = (
            Charge.objects.exclude(status=ChargeStatus.VOID)
            .filter(owner__isnull=False)
            .values("owner")
            .annotate(total=Sum("amount"), paid=Sum("paid_amount"))
        )
        total_debt = sum(
            (row["total"] or Decimal("0")) - (row["paid"] or Decimal("0")) for row in total_balance
        )

        return Response(
            {
                "period": {"label": period.label, "title": period.title,
                           "due_date": period.due_date.isoformat(), "is_open": period.is_open},
                "trade_points": {
                    "total": TradePoint.objects.count(),
                    "active": TradePoint.objects.filter(is_active=True).count(),
                    "with_meters": TradePoint.objects.filter(meters__is_active=True).distinct().count(),
                },
                "meters": {
                    "total": Meter.objects.count(),
                    "active": Meter.objects.filter(is_active=True).count(),
                },
                "users": {
                    "total": User.objects.count(),
                    "tenants": User.objects.filter(role=UserRole.TENANT, is_active=True).count(),
                    "staff": User.objects.filter(
                        role__in=[UserRole.CONTROLLER, UserRole.ADMIN, UserRole.SUPER_ADMIN],
                        is_active=True,
                    ).count(),
                },
                "charges": {
                    "count": aggregates["count"],
                    "charged": str(charged),
                    "paid": str(paid),
                    "remaining": str(max(charged - paid, Decimal("0"))),
                    "currency": settings.PAYMENT_CURRENCY,
                },
                "readings": reading_stats,
                "payments_today": {
                    "count": payments.count(),
                    "collected": str(today_collected),
                },
                "total_debt": str(max(total_debt, Decimal("0"))),
                "unread_notifications": Notification.objects.filter(is_read=False).count(),
            }
        )


def _period_from_request(request) -> BillingPeriod | None:
    label = request.query_params.get("period_label")
    if label:
        try:
            year, month = label.split("-")
            return BillingPeriod.objects.filter(year=int(year), month=int(month)).first()
        except (ValueError, AttributeError):
            return None
    period_id = request.query_params.get("period")
    if period_id:
        return BillingPeriod.objects.filter(pk=period_id).first()
    return None


@extend_schema(tags=["reports"], summary="Экспорт потребления в CSV")
class ConsumptionExportView(APIView):
    permission_classes = [IsStaffRole]

    def get(self, request):
        period = _period_from_request(request) or current_period()
        point_code = request.query_params.get("trade_point_code")
        charges = (
            Charge.objects.filter(period=period)
            .exclude(status=ChargeStatus.VOID)
            .select_related("trade_point", "owner", "meter")
            .order_by("trade_point__code")
        )
        if point_code:
            charges = charges.filter(trade_point__code=point_code)

        header = [
            "Период", "Код точки", "Точка", "Счётчик", "Плательщик",
            "Предыдущее", "Текущее", "кВт⋅ч", "Тариф", "Начислено", "Оплачено", "Статус",
        ]
        rows = (
            [
                period.label,
                charge.trade_point.code,
                charge.trade_point.name,
                charge.meter.serial_number if charge.meter else "",
                str(charge.owner or ""),
                charge.previous_value,
                charge.current_value,
                charge.consumption,
                charge.tariff_price,
                charge.amount,
                charge.paid_amount,
                charge.get_status_display(),
            ]
            for charge in charges
        )
        return csv_response(f"consumption-{period.label}.csv", header, rows)


@extend_schema(tags=["reports"], summary="Экспорт задолженности в CSV")
class DebtExportView(APIView):
    permission_classes = [IsStaffRole]

    def get(self, request):
        rows_qs = (
            Charge.objects.exclude(status=ChargeStatus.VOID)
            .values(
                "owner_id",
                "owner__phone",
                "owner__last_name",
                "owner__first_name",
                "trade_point__code",
                "trade_point__name",
            )
            .annotate(total=Sum("amount"), paid=Sum("paid_amount"))
            .order_by("owner__last_name")
        )
        header = ["Телефон", "Арендатор", "Код точки", "Точка", "Начислено", "Оплачено", "Задолженность"]
        rows = []
        for row in rows_qs:
            remaining = (row["total"] or Decimal("0")) - (row["paid"] or Decimal("0"))
            if remaining <= 0:
                continue
            name = " ".join(part for part in [row["owner__last_name"], row["owner__first_name"]] if part)
            rows.append([
                row["owner__phone"], name, row["trade_point__code"], row["trade_point__name"],
                str(money(row["total"])), str(money(row["paid"])), str(money(remaining)),
            ])
        return csv_response("debts.csv", header, rows)


@extend_schema(tags=["reports"], summary="Экспорт платежей в CSV")
class PaymentsExportView(APIView):
    permission_classes = [IsStaffRole]

    def get(self, request):
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        payments = Payment.objects.select_related("user", "trade_point").order_by("-created_at")
        if date_from:
            payments = payments.filter(created_at__date__gte=date_from)
        if date_to:
            payments = payments.filter(created_at__date__lte=date_to)

        header = ["Дата", "Плательщик", "Телефон", "Точка", "Сумма", "Валюта", "Статус", "Провайдер",
                  "ID в банке", "Квитанция"]
        rows = (
            [
                payment.created_at.strftime("%Y-%m-%d %H:%M"),
                str(payment.user),
                getattr(payment.user, "phone", ""),
                payment.trade_point.code if payment.trade_point else "",
                payment.amount,
                payment.currency,
                payment.get_status_display(),
                payment.provider,
                payment.provider_payment_id,
                payment.receipt.number if hasattr(payment, "receipt") else "",
            ]
            for payment in payments
        )
        return csv_response("payments.csv", header, rows)
