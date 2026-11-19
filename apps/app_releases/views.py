"""GET /api/v1/app/version-check/ — нужно ли клиенту обновиться."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.utils import request_language

from .flags import features_payload
from .serializers import FeaturesResponseSerializer, VersionCheckQuerySerializer, VersionCheckResponseSerializer
from .services import RolloutSubject, record_check_in, resolve_update
from .throttling import FeatureFlagThrottle, VersionCheckThrottle


class VersionCheckView(APIView):
    # Нет заголовка Authorization — аноним; невалидный/истёкший токен — 401 (клиент обновит токен).
    authentication_classes = [JWTAuthentication]
    permission_classes = [AllowAny]
    throttle_classes = [VersionCheckThrottle, AnonRateThrottle]

    @extend_schema(
        tags=["app"],
        summary="Проверка обновления приложения",
        description=(
            "Поэтапная раскатка. Авторизация необязательна (без заголовка — анонимно; невалидный токен — 401): при валидном JWT ключ раскатки — "
            "пользователь, иначе `device_id`. Попадание в раскатку детерминировано и не меняется "
            "при росте процента. `force_update=true`, если сборка клиента ниже `min_supported_build` "
            "(независимо от процента) или целевой релиз обязательный."
        ),
        parameters=[
            OpenApiParameter("platform", OpenApiTypes.STR, enum=["android", "ios"], required=True),
            OpenApiParameter("build", OpenApiTypes.INT, required=True, description="Номер сборки клиента"),
            OpenApiParameter("version", OpenApiTypes.STR, required=False, description="Версия клиента, напр. 1.2.0"),
            OpenApiParameter("device_id", OpenApiTypes.STR, required=False, description="ID установки"),
            OpenApiParameter("lang", OpenApiTypes.STR, enum=["ru", "ky"], required=False),
        ],
        responses={200: VersionCheckResponseSerializer},
        examples=[
            OpenApiExample(
                "Доступно обновление",
                value={
                    "update_available": True,
                    "force_update": False,
                    "latest_version": "1.3.0",
                    "latest_build": 13,
                    "min_supported_build": 10,
                    "store_url": "https://play.google.com/store/apps/details?id=kg.oshbazar.electro",
                    "release_notes": "Что нового: ...",
                    "rollout_percentage": 25,
                },
                response_only=True,
            ),
            OpenApiExample(
                "Обновлений нет",
                value={
                    "update_available": False,
                    "force_update": False,
                    "latest_version": None,
                    "latest_build": None,
                    "min_supported_build": None,
                    "store_url": None,
                    "release_notes": None,
                    "rollout_percentage": None,
                },
                response_only=True,
            ),
        ],
    )
    def get(self, request):
        query = VersionCheckQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        user = request.user if request.user and request.user.is_authenticated else None
        subject = RolloutSubject(user=user, device_id=data["device_id"].strip())
        decision = resolve_update(data["platform"], data["build"], subject)
        record_check_in(
            platform=data["platform"],
            build=data["build"],
            version=data["version"],
            subject=subject,
            offered=decision.release,
        )
        return Response(decision.as_payload(request_language(request)))


class FeaturesView(APIView):
    """GET /api/v1/app/features/ — значения фича-флагов для пользователя/устройства."""

    # Нет заголовка Authorization — аноним; невалидный/истёкший токен — 401 (клиент обновит токен).
    authentication_classes = [JWTAuthentication]
    permission_classes = [AllowAny]
    throttle_classes = [FeatureFlagThrottle, AnonRateThrottle]

    @extend_schema(
        tags=["app"],
        summary="Фича-флаги для пользователя",
        description=(
            "Авторизация необязательна (без заголовка — анонимно; невалидный токен — 401): при валидном JWT ключ — пользователь, иначе `device_id` (UUID). "
            "Попадание детерминировано: `sha256(\"{flag_key}:{rollout_key}\") % 100 < процент`, "
            "поэтому рост процента только добавляет пользователей. В `flags` — все активные флаги "
            "(true/false), в `payloads` — только для флагов со значением true и заданным payload."
        ),
        parameters=[
            OpenApiParameter("platform", OpenApiTypes.STR, enum=["android", "ios"], required=True),
            OpenApiParameter("build", OpenApiTypes.INT, required=True, description="Номер сборки клиента"),
            OpenApiParameter("version", OpenApiTypes.STR, required=False),
            OpenApiParameter("device_id", OpenApiTypes.STR, required=False, description="ID установки"),
        ],
        responses={200: FeaturesResponseSerializer},
        examples=[
            OpenApiExample(
                "Флаги",
                value={
                    "flags": {"new_payment_screen": True, "dark_map": False},
                    "payloads": {"new_payment_screen": {"variant": "b"}},
                    "ttl_seconds": 300,
                },
                response_only=True,
            )
        ],
    )
    def get(self, request):
        query = VersionCheckQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        user = request.user if request.user and request.user.is_authenticated else None
        subject = RolloutSubject(user=user, device_id=data["device_id"].strip())
        payload = features_payload(subject, platform=data["platform"], build=data["build"])
        record_check_in(platform=data["platform"], build=data["build"], version=data["version"], subject=subject)
        response = Response(payload)
        response["Cache-Control"] = "private, no-store"
        return response
