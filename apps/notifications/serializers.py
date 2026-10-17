"""Serializers for notifications and push devices."""
from rest_framework import serializers

from .models import DeviceToken, Notification


class NotificationSerializer(serializers.ModelSerializer):
    type_display = serializers.CharField(source="get_type_display", read_only=True)

    class Meta:
        model = Notification
        fields = ["id", "type", "type_display", "title", "body", "params", "is_read",
                  "read_at", "created_at"]
        read_only_fields = fields


class DeviceTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceToken
        fields = ["id", "token", "platform", "locale", "is_active", "last_seen_at", "created_at"]
        read_only_fields = ["id", "last_seen_at", "created_at"]

    def create(self, validated_data: dict) -> DeviceToken:
        user = self.context["request"].user
        token = validated_data["token"]
        device, _ = DeviceToken.objects.update_or_create(
            token=token,
            defaults={
                "user": user,
                "platform": validated_data.get("platform", DeviceToken._meta.get_field("platform").default),
                "locale": validated_data.get("locale", getattr(user, "language", "ru")),
                "is_active": True,
            },
        )
        return device


class UnreadCountSerializer(serializers.Serializer):
    unread = serializers.IntegerField()
