"""Serializers for meter readings, photos and review flow."""
from decimal import Decimal

from django.conf import settings
from django.urls import reverse
from rest_framework import serializers

from apps.trade_points.models import Meter

from .models import MeterReading, ReadingPhoto, ReadingSource
from .services import previous_value_for


class ReadingPhotoSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    file = serializers.SerializerMethodField()
    uploaded_by_name = serializers.CharField(source="uploaded_by.__str__", read_only=True, default=None)
    trade_point_code = serializers.CharField(source="trade_point.code", read_only=True)

    class Meta:
        model = ReadingPhoto
        fields = ["id", "url", "file", "content_type", "size_bytes", "trade_point",
                  "trade_point_code", "meter", "original_value", "uploaded_by",
                  "uploaded_by_name", "uploaded_at"]
        read_only_fields = [f for f in fields if f != "file"]

    def get_url(self, obj: ReadingPhoto) -> str | None:
        request = self.context.get("request")
        if not request or not obj.file:
            return None
        return request.build_absolute_uri(reverse("readings:photo-file", kwargs={"pk": obj.pk}))

    def get_file(self, obj: ReadingPhoto) -> str | None:
        return self.get_url(obj)


class MeterReadingSerializer(serializers.ModelSerializer):
    period_label = serializers.CharField(source="period.label", read_only=True)
    period_title = serializers.CharField(source="period.title", read_only=True)
    trade_point_code = serializers.CharField(source="trade_point.code", read_only=True)
    trade_point_name = serializers.CharField(source="trade_point.name", read_only=True)
    meter_serial = serializers.CharField(source="meter.serial_number", read_only=True)
    submitted_by_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    consumption = serializers.SerializerMethodField()
    due_date = serializers.DateField(source="period.due_date", read_only=True)
    photo_data = ReadingPhotoSerializer(source="photo", read_only=True)

    class Meta:
        model = MeterReading
        fields = [
            "id", "meter", "meter_serial", "trade_point", "trade_point_code", "trade_point_name",
            "period", "period_label", "period_title", "due_date", "previous_value", "value",
            "consumption", "photo", "photo_data", "status", "status_display", "source",
            "source_display", "rejection_reason", "reviewed_by", "reviewed_at", "submitted_by",
            "submitted_by_name", "submitted_at",
        ]
        read_only_fields = [f for f in fields if f not in ("photo",)]

    def get_submitted_by_name(self, obj: MeterReading) -> str:
        return str(obj.submitted_by) if obj.submitted_by else ""

    def get_consumption(self, obj: MeterReading) -> str:
        return str(obj.consumption)


class ReadingSubmitSerializer(serializers.Serializer):
    """Передача показания арендатором (ТЗ, п. 8)."""

    meter = serializers.PrimaryKeyRelatedField(queryset=Meter.objects.filter(is_active=True))
    value = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"))
    photo = serializers.ImageField(required=False, allow_null=True)
    period = serializers.UUIDField(required=False, allow_null=True)

    def validate_value(self, value: Decimal) -> Decimal:
        if value is None:
            raise serializers.ValidationError("Введите текущее показание.")
        return value

    def validate(self, attrs: dict) -> dict:
        from apps.billing.models import BillingPeriod
        from apps.trade_points.services import accessible_trade_points

        meter = attrs["meter"]
        request = self.context["request"]
        if not accessible_trade_points(request.user).filter(pk=meter.trade_point_id).exists():
            raise serializers.ValidationError(
                {"meter": "У вас нет доступа к этой торговой точке."}
            )
        if attrs.get("period"):
            try:
                attrs["period"] = BillingPeriod.objects.get(pk=attrs["period"])
            except BillingPeriod.DoesNotExist as exc:
                raise serializers.ValidationError({"period": "Расчётный период не найден."}) from exc
        else:
            attrs["period"] = None

        # Мгновенная понятная ошибка до записи в БД.
        period = attrs["period"]
        from apps.billing.services import current_period

        period = period or current_period()
        previous = previous_value_for(meter, period)
        value = attrs["value"]
        if value < previous and not getattr(settings, "READINGS_ALLOW_DECREASE", False):
            raise serializers.ValidationError(
                {
                    "value": (
                        f"Текущее показание ({value}) меньше предыдущего ({previous}). "
                        "Проверьте введённые данные."
                    )
                }
            )
        attrs["previous_value"] = previous
        return attrs

    def create(self, validated_data: dict) -> MeterReading:
        from .services import submit_reading

        period = validated_data.pop("period", None)
        return submit_reading(
            user=self.context["request"].user,
            meter=validated_data["meter"],
            value=validated_data["value"],
            photo_file=validated_data.get("photo"),
            period=period,
            source=ReadingSource.TENANT,
        )


class ReadingPreviewSerializer(serializers.Serializer):
    """Предварительный расчёт потребления и суммы (ТЗ, п. 8, шаг 5)."""

    meter = serializers.PrimaryKeyRelatedField(queryset=Meter.objects.filter(is_active=True))
    value = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"))

    def to_representation(self, instance) -> dict:
        return instance


class ReadingReviewSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=[("approve", "approve"), ("reject", "reject")])
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class ReadingStatsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    pending = serializers.IntegerField()
    approved = serializers.IntegerField()
    rejected = serializers.IntegerField()
    period = serializers.CharField()
