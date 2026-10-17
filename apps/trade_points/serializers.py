"""Serializers for trade points, meters and access management."""
from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import AccessRole, Meter, TradePoint, TradePointAccess

User = get_user_model()


class MeterSerializer(serializers.ModelSerializer):
    trade_point_code = serializers.CharField(source="trade_point.code", read_only=True)
    last_value = serializers.SerializerMethodField()

    class Meta:
        model = Meter
        fields = [
            "id",
            "trade_point",
            "trade_point_code",
            "serial_number",
            "model_name",
            "initial_reading",
            "installed_at",
            "deactivated_at",
            "is_primary",
            "is_active",
            "external_id_1c",
            "last_value",
            "created_at",
        ]
        read_only_fields = ["id", "created_at", "external_id_1c"]

    def get_last_value(self, obj: Meter):
        reading = obj.readings.filter(status="approved").order_by("-period__year", "-period__month").first()
        if reading:
            return reading.value
        return obj.initial_reading


class TradePointAccessSerializer(serializers.ModelSerializer):
    user_info = serializers.SerializerMethodField()
    trade_point_info = serializers.SerializerMethodField()

    class Meta:
        model = TradePointAccess
        fields = [
            "id",
            "user",
            "user_info",
            "trade_point",
            "trade_point_info",
            "access_role",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def get_user_info(self, obj: TradePointAccess) -> str:
        return str(obj.user)

    def get_trade_point_info(self, obj: TradePointAccess) -> str:
        return str(obj.trade_point)


class TradePointSerializer(serializers.ModelSerializer):
    meters = MeterSerializer(many=True, read_only=True)
    primary_meter = serializers.SerializerMethodField()
    qr_url = serializers.SerializerMethodField()
    tenant_count = serializers.SerializerMethodField()
    my_access_role = serializers.SerializerMethodField()

    class Meta:
        model = TradePoint
        fields = [
            "id",
            "code",
            "name",
            "block",
            "place_number",
            "address",
            "area_m2",
            "qr_payload",
            "qr_url",
            "external_id_1c",
            "is_active",
            "meters",
            "primary_meter",
            "tenant_count",
            "my_access_role",
            "created_at",
        ]
        read_only_fields = ["id", "qr_url", "external_id_1c", "created_at"]

    def get_primary_meter(self, obj: TradePoint):
        meter = next((m for m in obj.meters.all() if m.is_primary and m.is_active), None)
        return MeterSerializer(meter).data if meter else None

    def get_tenant_count(self, obj: TradePoint) -> int:
        annotated = getattr(obj, "tenant_count", None)
        if annotated is not None:
            return annotated
        return obj.user_accesses.filter(is_active=True).count()

    def get_qr_url(self, obj: TradePoint) -> str | None:
        request = self.context.get("request")
        if not request:
            return None
        from rest_framework.reverse import reverse

        return request.build_absolute_uri(reverse("trade-points:qr", args=[obj.id]))

    def get_my_access_role(self, obj: TradePoint) -> str | None:
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        for access in obj.user_accesses.all():
            if access.user_id == request.user.id:
                return access.access_role
        return None


class TradePointCreateSerializer(serializers.ModelSerializer):
    tenants = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        many=True,
        required=False,
        write_only=True,
        help_text="ID пользователей, которым открывается доступ к точке.",
    )

    class Meta:
        model = TradePoint
        fields = [
            "id",
            "code",
            "name",
            "block",
            "place_number",
            "address",
            "area_m2",
            "qr_payload",
            "is_active",
            "tenants",
        ]
        read_only_fields = ["id"]

    def validate_code(self, value: str) -> str:
        queryset = TradePoint.objects.filter(code=value)
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError("Торговая точка с таким кодом уже существует.")
        return value

    def create(self, validated_data: dict) -> TradePoint:
        tenants = validated_data.pop("tenants", [])
        point = super().create(validated_data)
        self._sync_access(point, tenants)
        return point

    def update(self, instance: TradePoint, validated_data: dict) -> TradePoint:
        tenants = validated_data.pop("tenants", None)
        point = super().update(instance, validated_data)
        if tenants is not None:
            self._sync_access(point, tenants)
        return point

    def _sync_access(self, point: TradePoint, users) -> None:
        if not users:
            return
        existing = {access.user_id: access for access in point.user_accesses.all()}
        granted_by = self.context.get("request").user if self.context.get("request") else None
        for user in users:
            access = existing.get(user.id)
            if access:
                access.is_active = True
                access.save(update_fields=["is_active", "updated_at"])
            else:
                TradePointAccess.objects.create(
                    user=user,
                    trade_point=point,
                    access_role=AccessRole.TENANT,
                    granted_by=granted_by,
                )


class QRCodeSerializer(serializers.Serializer):
    trade_point = serializers.CharField(read_only=True)
    code = serializers.CharField(read_only=True)
    payload = serializers.CharField(read_only=True)
    image_url = serializers.CharField(read_only=True)


class ScanResultSerializer(serializers.Serializer):
    found = serializers.BooleanField(read_only=True)
    trade_point = TradePointSerializer(read_only=True)
    is_accessible = serializers.BooleanField(read_only=True)
    meters = MeterSerializer(many=True, read_only=True)
