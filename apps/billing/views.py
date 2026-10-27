"""Billing endpoints: periods, tariffs, charges, debt, ledger, consumption."""
import django_filters
from django.db.models import Sum
from django.utils.translation import gettext as _
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import filters, generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.audit import log_action
from apps.core.permissions import IsAdminRole, IsStaffRole
from apps.trade_points.services import accessible_trade_points, get_trade_point_for_user

from .models import BillingPeriod, Charge, ChargeStatus, LedgerEntry, Tariff
from .serializers import (
    BillingPeriodSerializer,
    ChargeCreateSerializer,
    ChargeSerializer,
    ConsumptionRowSerializer,
    DebtSummarySerializer,
    LedgerEntrySerializer,
    TariffSerializer,
)
from .services import (
    consumption_history,
    consumption_history_for_user,
    current_period,
    debt_summary,
    recalculate_charge,
    void_charge,
)


class ChargeFilter(django_filters.FilterSet):
    period = django_filters.UUIDFilter(field_name="period_id")
    period_label = django_filters.CharFilter(field_name="period__label")
    status = django_filters.ChoiceFilter(choices=ChargeStatus.choices)
    trade_point = django_filters.UUIDFilter(field_name="trade_point_id")
    trade_point_code = django_filters.CharFilter(field_name="trade_point__code")
    due_before = django_filters.DateFilter(field_name="due_date", lookup_expr="lte")
    due_after = django_filters.DateFilter(field_name="due_date", lookup_expr="gte")

    class Meta:
        model = Charge
        fields = ["period", "period_label", "status", "trade_point", "trade_point_code"]


@extend_schema_view(
    list=extend_schema(tags=["billing"], summary="Расчётные периоды"),
    create=extend_schema(tags=["staff"], summary="Создать расчётный период (админ)"),
)
class PeriodListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsStaffRole]
    serializer_class = BillingPeriodSerializer
    filterset_fields = ["year", "month", "is_open"]
    ordering_fields = ["year", "month"]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsStaffRole()]

    def get_queryset(self):
        return BillingPeriod.objects.all()


@extend_schema(tags=["billing"], summary="Закрыть расчётный период (админ)")
class PeriodCloseView(APIView):
    permission_classes = [IsAdminRole]

    def post(self, request, pk):
        from .services import close_period

        try:
            period = BillingPeriod.objects.get(pk=pk)
        except BillingPeriod.DoesNotExist:
            return Response(
                {"errors": [{"code": "not_found", "message": _("Период не найден."), "field": None}]},
                status=status.HTTP_404_NOT_FOUND,
            )
        result = close_period(period, actor=request.user)
        log_action(user=request.user, action="billing.period_closed", target=period, request=request)
        return Response(result)


@extend_schema_view(
    list=extend_schema(tags=["billing"], summary="Тарифы (текущий тариф для расчётов)"),
    create=extend_schema(tags=["staff"], summary="Создать тариф (админ)"),
)
class TariffListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsStaffRole]
    serializer_class = TariffSerializer
    filterset_fields = ["is_active"]
    ordering_fields = ["valid_from", "price", "created_at"]

    def get_queryset(self):
        return Tariff.objects.select_related("created_by")

    def perform_create(self, serializer):
        tariff = serializer.save(created_by=self.request.user)
        log_action(user=self.request.user, action="billing.tariff_created", target=tariff, request=self.request)


@extend_schema_view(
    retrieve=extend_schema(tags=["billing"], summary="Тариф"),
    update=extend_schema(tags=["staff"], summary="Изменить тариф (админ)"),
    partial_update=extend_schema(tags=["staff"], summary="Изменить тариф (админ)"),
    destroy=extend_schema(tags=["staff"], summary="Отключить тариф (админ)"),
)
class TariffDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsStaffRole]
    serializer_class = TariffSerializer
    queryset = Tariff.objects.all()

    def perform_destroy(self, instance: Tariff):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        log_action(user=self.request.user, action="billing.tariff_disabled", target=instance,
                   request=self.request)


@extend_schema_view(
    list=extend_schema(
        tags=["billing"],
        summary="Начисления",
        description="Арендатор видит только свои начисления, персонал — все с фильтрами.",
    ),
    create=extend_schema(tags=["staff"], summary="Ручное начисление (админ)"),
)
class ChargeListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    filterset_class = ChargeFilter
    search_fields = ["trade_point__code", "trade_point__name", "owner__phone", "owner__last_name"]
    ordering_fields = ["created_at", "due_date", "amount", "period__year", "period__month"]
    ordering = ["-created_at"]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ChargeCreateSerializer
        return ChargeSerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminRole()]
        return [IsAuthenticated()]

    def get_queryset(self):
        qs = Charge.objects.select_related("period", "trade_point", "meter", "owner", "tariff")
        user = self.request.user
        if not user.is_staff_role:
            qs = qs.filter(owner=user)
        return qs

    def perform_create(self, serializer):
        charge = serializer.save()
        from apps.billing.services import write_ledger

        write_ledger(
            user=charge.owner,
            kind=LedgerEntry.Kind.CHARGE,
            amount=charge.amount,
            trade_point=charge.trade_point,
            charge=charge,
            source=LedgerEntry.Source.MANUAL,
            note=f"Ручное начисление за {charge.period.label}",
        )
        log_action(user=self.request.user, action="billing.charge_created", target=charge,
                   request=self.request)


@extend_schema(tags=["billing"], summary="Начисление")
class ChargeDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ChargeSerializer
    queryset = Charge.objects.select_related("period", "trade_point", "meter", "owner", "tariff")

    def get_object(self):
        obj = super().get_object()
        if not self.request.user.is_staff_role and obj.owner_id != self.request.user.id:
            from rest_framework.exceptions import NotFound

            raise NotFound(_("Начисление не найдено."))
        return obj


@extend_schema(tags=["staff"], summary="Аннулировать начисление (админ)")
class ChargeVoidView(APIView):
    permission_classes = [IsAdminRole]

    def post(self, request, pk):
        try:
            charge = Charge.objects.select_related("period", "trade_point", "owner").get(pk=pk)
        except Charge.DoesNotExist:
            return Response(
                {"errors": [{"code": "not_found", "message": _("Начисление не найдено."), "field": None}]},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            void_charge(charge, actor=request.user, reason=str(request.data.get("reason", "")))
        except ValueError:
            return Response(
                {
                    "errors": [
                        {
                            "code": "charge_already_paid",
                            "message": _("Нельзя аннулировать начисление с оплатой."),
                            "field": None,
                        }
                    ]
                },
                status=status.HTTP_409_CONFLICT,
            )
        log_action(user=request.user, action="billing.charge_voided", target=charge, request=request)
        return Response(ChargeSerializer(charge).data)


@extend_schema(tags=["staff"], summary="Пересчитать начисление по тарифу (админ)")
class ChargeRecalculateView(APIView):
    permission_classes = [IsAdminRole]

    def post(self, request, pk):
        try:
            charge = Charge.objects.select_related("period", "trade_point", "owner", "tariff").get(pk=pk)
        except Charge.DoesNotExist:
            return Response(
                {"errors": [{"code": "not_found", "message": _("Начисление не найдено."), "field": None}]},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            charge = recalculate_charge(charge, actor=request.user)
        except ValueError:
            return Response(
                {
                    "errors": [
                        {
                            "code": "charge_already_paid",
                            "message": _("Нельзя пересчитать начисление с оплатой."),
                            "field": None,
                        }
                    ]
                },
                status=status.HTTP_409_CONFLICT,
            )
        log_action(user=request.user, action="billing.charge_recalculated", target=charge, request=request)
        return Response(ChargeSerializer(charge).data)


@extend_schema(
    tags=["billing"],
    summary="Задолженность и аванс",
    description="Общая задолженность, переплата, разбивка по периодам и торговым точкам.",
    responses=DebtSummarySerializer,
)
class DebtView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        point = None
        point_id = request.query_params.get("trade_point")
        if point_id:
            point = get_trade_point_for_user(request.user, point_id)
        return Response(debt_summary(request.user, trade_point=point))


@extend_schema(
    tags=["billing"],
    summary="История финансовых операций (журнал)",
    description="Неизменяемый журнал начислений и платежей с балансом после каждой операции.",
    responses=LedgerEntrySerializer(many=True),
)
class LedgerView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = LedgerEntrySerializer
    filterset_fields = ["kind", "trade_point", "charge"]
    ordering_fields = ["created_at", "amount"]
    ordering = ["-created_at"]

    def get_queryset(self):
        qs = LedgerEntry.objects.select_related("trade_point", "charge")
        user = self.request.user
        if not user.is_staff_role:
            return qs.filter(user=user)
        user_id = self.request.query_params.get("user")
        return qs.filter(user=user_id) if user_id else qs


@extend_schema(
    tags=["consumption"],
    summary="История потребления по месяцам",
    description=(
        "Данные для графика и таблицы потребления: показания, кВт⋅ч, тариф, "
        "начисление и статус оплаты по каждому периоду."
    ),
    responses=ConsumptionRowSerializer(many=True),
)
class ConsumptionView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        months = min(int(request.query_params.get("months", 12)), 36)
        point_id = request.query_params.get("trade_point")
        if point_id:
            point = get_trade_point_for_user(request.user, point_id)
            rows = consumption_history(point, months=months)
        else:
            rows = consumption_history_for_user(request.user, months=months)
        return Response(rows)


@extend_schema(tags=["billing"], summary="Текущий расчётный период")
class CurrentPeriodView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        period = current_period()
        serializer = BillingPeriodSerializer(period)
        data = serializer.data
        data["open_periods"] = BillingPeriodSerializer(
            BillingPeriod.objects.filter(is_open=True), many=True
        ).data
        data["total_charges"] = Charge.objects.filter(period=period).aggregate(
            total=Sum("amount")
        )["total"] or "0"
        return Response(data)
