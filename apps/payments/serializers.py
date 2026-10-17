"""Serializers for payments, allocations and receipts."""
from decimal import Decimal

from django.conf import settings
from rest_framework import serializers

from apps.billing.models import Charge, ChargeStatus
from apps.billing.services import money
from apps.trade_points.services import accessible_trade_points

from .models import Payment, PaymentAllocation, PaymentStatus, Receipt
from .providers.registry import available_providers


class PaymentAllocationSerializer(serializers.ModelSerializer):
    period_label = serializers.CharField(source="charge.period.label", read_only=True)
    period_title = serializers.CharField(source="charge.period.title", read_only=True)
    trade_point_code = serializers.CharField(source="charge.trade_point.code", read_only=True)

    class Meta:
        model = PaymentAllocation
        fields = ["id", "charge", "period_label", "period_title", "trade_point_code", "amount", "created_at"]
        read_only_fields = fields


class ReceiptSerializer(serializers.ModelSerializer):
    payment_id = serializers.UUIDField(read_only=True)
    amount = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()

    class Meta:
        model = Receipt
        fields = ["id", "number", "payment_id", "amount", "currency", "data", "issued_at"]
        read_only_fields = fields

    def get_amount(self, obj: Receipt) -> str:
        return str(obj.payment.amount)

    def get_currency(self, obj: Receipt) -> str:
        return obj.payment.currency


class PaymentSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    user_name = serializers.SerializerMethodField()
    trade_point_code = serializers.CharField(source="trade_point.code", read_only=True, default=None)
    allocations = PaymentAllocationSerializer(many=True, read_only=True)
    receipt_id = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id", "user", "user_name", "trade_point", "trade_point_code", "amount", "currency",
            "status", "status_display", "provider", "provider_payment_id", "redirect_url",
            "description", "target_charge_ids", "failure_reason", "initiated_at",
            "completed_at", "allocations", "receipt_id", "created_at",
        ]
        read_only_fields = fields

    def get_user_name(self, obj: Payment) -> str:
        return str(obj.user)

    def get_receipt_id(self, obj: Payment):
        receipt = getattr(obj, "receipt", None)
        return str(receipt.id) if receipt else None


class PaymentInitiateSerializer(serializers.Serializer):
    """Инициирование оплаты через банк (ТЗ, п. 13)."""

    provider = serializers.ChoiceField(choices=[], required=False)
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01"), required=False
    )
    charge_ids = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    trade_point = serializers.UUIDField(required=False, allow_null=True)
    description = serializers.CharField(required=False, allow_blank=True, max_length=255)
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=128)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        providers = list(available_providers().keys())
        self.fields["provider"].choices = [(code, code) for code in providers]

    def validate(self, attrs: dict) -> dict:
        request = self.context["request"]
        user = request.user

        charge_ids = [str(cid) for cid in attrs.get("charge_ids", [])]
        if charge_ids:
            charges = Charge.objects.filter(
                id__in=charge_ids, owner=user
            ).exclude(status=ChargeStatus.VOID)
            found = {str(c.id) for c in charges}
            unknown = set(charge_ids) - found
            if unknown:
                raise serializers.ValidationError(
                    {"charge_ids": f"Начисления не найдены или недоступны: {', '.join(unknown)}"}
                )
            pending_total = sum((c.remaining for c in charges), Decimal("0"))
            if not attrs.get("amount"):
                if pending_total <= 0:
                    raise serializers.ValidationError(
                        {"amount": "По выбранным начислениям нет суммы к оплате."}
                    )
                attrs["amount"] = money(pending_total)
            attrs["charges"] = list(charges)

        if not attrs.get("amount"):
            raise serializers.ValidationError({"amount": "Укажите сумму платежа."})

        if attrs.get("trade_point"):
            from apps.trade_points.models import TradePoint

            point = TradePoint.objects.filter(pk=attrs["trade_point"]).first()
            if point is None or not accessible_trade_points(user).filter(pk=point.pk).exists():
                raise serializers.ValidationError({"trade_point": "Торговая точка недоступна."})
            attrs["trade_point_obj"] = point

        return attrs
