import uuid

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

MAX_BUILD = 2_147_483_647  # предел integer в PostgreSQL

from .models import Platform


class VersionCheckQuerySerializer(serializers.Serializer):
    platform = serializers.ChoiceField(choices=Platform.choices, help_text=_("android или ios"))
    build = serializers.IntegerField(min_value=0, max_value=MAX_BUILD, help_text=_("Номер сборки клиента (versionCode / CFBundleVersion)"))
    version = serializers.CharField(required=False, allow_blank=True, max_length=32, default="")
    device_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=128,
        default="",
        help_text=_("UUID установки; ключ раскатки для анонимных запросов"),
    )

    def validate_device_id(self, value):
        value = (value or "").strip()
        if not value:
            return ""
        try:
            return str(uuid.UUID(value))  # канонический вид: нижний регистр, с дефисами
        except ValueError:
            raise serializers.ValidationError(_("device_id должен быть UUID.")) from None


class VersionCheckResponseSerializer(serializers.Serializer):
    update_available = serializers.BooleanField()
    force_update = serializers.BooleanField()
    latest_version = serializers.CharField(allow_null=True)
    latest_build = serializers.IntegerField(allow_null=True)
    min_supported_build = serializers.IntegerField(allow_null=True)
    store_url = serializers.URLField(allow_null=True)
    release_notes = serializers.CharField(allow_null=True)
    rollout_percentage = serializers.IntegerField(allow_null=True, min_value=0, max_value=100)


class FeaturesResponseSerializer(serializers.Serializer):
    flags = serializers.DictField(
        child=serializers.BooleanField(), help_text=_("Каждый активный флаг: true/false для этого пользователя")
    )
    payloads = serializers.DictField(
        child=serializers.JSONField(), help_text=_("Только для флагов со значением true и заданным payload")
    )
    ttl_seconds = serializers.IntegerField(help_text=_("Сколько секунд клиент может кешировать ответ"))
