"""Serializers for the 1C integration."""
from rest_framework import serializers

from .models import ExternalObject, SyncEntity, SyncRun


class SyncRunSerializer(serializers.ModelSerializer):
    entity_display = serializers.CharField(source="get_entity_display", read_only=True)
    direction_display = serializers.CharField(source="get_direction_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    duration_ms = serializers.SerializerMethodField()

    class Meta:
        model = SyncRun
        fields = ["id", "entity", "entity_display", "direction", "direction_display", "status",
                  "status_display", "started_at", "finished_at", "duration_ms", "stats", "error"]
        read_only_fields = fields

    def get_duration_ms(self, obj: SyncRun):
        if not obj.finished_at:
            return None
        return int((obj.finished_at - obj.started_at).total_seconds() * 1000)


class SyncRequestSerializer(serializers.Serializer):
    entity = serializers.ChoiceField(
        choices=[(SyncEntity.TENANT, "tenant"), (SyncEntity.TRADE_POINT, "trade_point"),
                 (SyncEntity.METER, "meter")],
        default=SyncEntity.TRADE_POINT,
    )
    direction = serializers.ChoiceField(choices=["import", "export"], default="import")
    dry_run = serializers.BooleanField(default=False)


class ExternalObjectSerializer(serializers.ModelSerializer):
    entity_display = serializers.CharField(source="get_entity_display", read_only=True)

    class Meta:
        model = ExternalObject
        fields = ["id", "entity", "entity_display", "external_id", "internal_id", "checksum",
                  "synced_at", "payload"]
        read_only_fields = fields
