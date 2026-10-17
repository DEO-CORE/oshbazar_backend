"""Serializers for periods, tariffs, charges and debt reports."""
from rest_framework import serializers

from .models import BillingPeriod, Charge, ChargeStatus, LedgerEntry, Tariff


class BillingPeriodSerializer(serializers.ModelSerializer):
    label = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)
    due_date = serializers.DateField(read_only=True)

    class Meta:
        model = BillingPeriod
        fields = ["id", "year", "month", "label", "title", "starts_on", "ends_on", "due_date",
                  "is_open", "closed_at", "created_at"]
        read_only_fields = fields


class TariffSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tariff
        fields = ["id", "name", "price", "currency", "valid_from", "valid_to", "is_active",
                  "created_by", "created_at"]
        read_only_fields = ["id", "created_by", "created_at"]

    def validate(self, attrs: dict) -> dict:
        instance = self.instance
        valid_from = attrs.get("valid_from", instance.valid_from if instance else None)
        valid_to = attrs.get("valid_to", instance.valid_to if instance else None)
        if valid_from and valid_to and valid_to < valid_from:
            raise serializers.ValidationError({"valid_to": "Дата окончания раньше даты начала."})
        if attrs.get("price") is not None and attrs["price"] < 0:
            raise serializers.ValidationError({"price": "Тариф не может быть отрицательным."})
        return attrs


class ChargeSerializer(serializers.ModelSerializer):
    period_label = serializers.CharField(source="period.label", read_only=True)
    period_title = serializers.CharField(source="period.title", read_only=True)
    trade_point_code = serializers.CharField(source="trade_point.code", read_only=True)
    trade_point_name = serializers.CharField(source="trade_point.name", read_only=True)
    owner_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    remaining = serializers.SerializerMethodField()
    tariff_currency = serializers.CharField(source="tariff.currency", read_only=True, default=None)

    class Meta:
        model = Charge
        fields = [
            "id", "period", "period_label", "period_title", "trade_point", "trade_point_code",
            "trade_point_name", "meter", "owner", "owner_name", "source_reading_id",
            "previous_value", "current_value", "consumption", "tariff", "tariff_price",
            "tariff_currency", "amount", "paid_amount", "credit_applied", "remaining",
            "status", "status_display", "due_date", "paid_at", "is_manual", "note",
            "created_at",
        ]
        read_only_fields = [f for f in fields if f not in ("note",)]

    def get_owner_name(self, obj: Charge) -> str:
        return str(obj.owner) if obj.owner else ""

    def get_remaining(self, obj: Charge) -> str:
        return str(obj.remaining)


class ChargeCreateSerializer(serializers.ModelSerializer):
    """Ручное начисление администратором (исправления, штрафы, корректировки)."""

    class Meta:
        model = Charge
        fields = ["id", "period", "trade_point", "meter", "owner", "consumption",
                  "tariff_price", "amount", "due_date", "note"]
        read_only_fields = ["id"]

    def validate(self, attrs: dict) -> dict:
        amount = attrs.get("amount")
        if amount is None and attrs.get("consumption") is not None and attrs.get("tariff_price") is not None:
            attrs["amount"] = (attrs["consumption"] * attrs["tariff_price"]).quantize("0.01")
        if attrs.get("amount") is None:
            raise serializers.ValidationError({"amount": "Укажите сумму или потребление и тариф."})
        if attrs["amount"] <= 0:
            raise serializers.ValidationError({"amount": "Сумма начисления должна быть положительной."})
        if attrs.get("owner") is None and attrs.get("trade_point") is not None:
            from .services import resolve_owner

            attrs["owner"] = resolve_owner(attrs["trade_point"])
        if attrs.get("due_date") is None and attrs.get("period") is not None:
            attrs["due_date"] = attrs["period"].due_date
        attrs["is_manual"] = True
        return attrs


class LedgerEntrySerializer(serializers.ModelSerializer):
    trade_point_code = serializers.CharField(source="trade_point.code", read_only=True, default=None)
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)

    class Meta:
        model = LedgerEntry
        fields = ["id", "kind", "kind_display", "source", "source_display", "amount",
                  "balance_after", "trade_point", "trade_point_code", "charge", "payment_ref",
                  "note", "created_at"]
        read_only_fields = fields


class PeriodRowSerializer(serializers.Serializer):
    period = serializers.CharField()
    label = serializers.CharField()
    title = serializers.CharField()
    amount = serializers.CharField()
    paid = serializers.CharField()
    remaining = serializers.CharField()
    due_date = serializers.DateField(allow_null=True)
    status = serializers.CharField()


class PointRowSerializer(serializers.Serializer):
    trade_point = serializers.CharField()
    code = serializers.CharField()
    name = serializers.CharField()
    amount = serializers.CharField()
    paid = serializers.CharField()
    remaining = serializers.CharField()


class DebtSummarySerializer(serializers.Serializer):
    """Общая задолженность, аванс и разбивка (ТЗ, п. 11–12)."""

    currency = serializers.CharField()
    balance = serializers.CharField(help_text="Положительное — задолженность, отрицательное — аванс.")
    debt = serializers.CharField()
    credit = serializers.CharField()
    total_charged = serializers.CharField()
    total_paid = serializers.CharField()
    open_amount = serializers.CharField()
    open_periods_count = serializers.IntegerField()
    next_due_date = serializers.DateField(allow_null=True)
    by_period = PeriodRowSerializer(many=True)
    by_trade_point = PointRowSerializer(many=True)


class ConsumptionRowSerializer(serializers.Serializer):
    period = serializers.CharField()
    label = serializers.CharField()
    title = serializers.CharField()
    previous_value = serializers.CharField()
    current_value = serializers.CharField()
    consumption = serializers.CharField()
    tariff = serializers.CharField(allow_null=True)
    amount = serializers.CharField()
    paid = serializers.CharField()
    remaining = serializers.CharField()
    status = serializers.CharField()
    due_date = serializers.DateField(allow_null=True)
