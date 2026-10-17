"""Authentication, profile and staff user management endpoints."""
from django.utils import timezone
from django.utils.translation import gettext as _
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import filters, generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenVerifyView

from apps.core.audit import log_action
from apps.core.permissions import IsAdminRole, IsStaffRole

from .models import PinRecoveryRequest, User, UserRole
from .serializers import (
    ChangePinSerializer,
    LoginSerializer,
    PinRecoveryDecisionSerializer,
    PinRecoveryRequestSerializer,
    ProfileUpdateSerializer,
    UserAdminSerializer,
    UserSerializer,
)
from .services import authenticate_pin


def issue_tokens(user: User) -> dict:
    refresh = RefreshToken.for_user(user)
    refresh["role"] = user.role
    refresh["language"] = user.language
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
    }


@extend_schema(
    tags=["auth"],
    summary="Вход по ПИН-коду",
    description=(
        "Авторизация по идентификатору (телефон/логин) и ПИН-коду. "
        "После успеха возвращаются JWT-токены и профиль с ролью пользователя. "
        "После 5 неудачных попыток ПИН блокируется на 15 минут."
    ),
    responses={200: None},
)
class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_pin"

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        tokens = issue_tokens(user)
        log_action(user=user, action="auth.login", request=request)
        return Response(
            {
                "access": tokens["access"],
                "refresh": tokens["refresh"],
                "user": UserSerializer(user).data,
            }
        )


class LogoutView(APIView):
    """Blacklist the refresh token — завершение сессии."""

    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["auth"], summary="Выход (blacklist refresh-токена)",
                    request={"refresh": str})
    def post(self, request):
        raw = request.data.get("refresh")
        if not raw:
            return Response(
                {"errors": [{"code": "required", "message": _("Укажите refresh-токен."), "field": "refresh"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            token = RefreshToken(raw)
            token.blacklist()
        except (InvalidToken, TokenError):
            return Response(
                {"errors": [{"code": "invalid_token", "message": _("Некорректный токен."), "field": "refresh"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        log_action(user=request.user, action="auth.logout", request=request)
        return Response({"detail": _("Сессия завершена.")})


class CustomTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs: dict) -> dict:
        # Parse before rotation: the refresh token is blacklisted by super().validate().
        # A blacklisted/invalid token must surface as 401 (InvalidToken), not 500.
        user = None
        try:
            raw_token = RefreshToken(attrs["refresh"])
            user = User.objects.filter(pk=raw_token.payload.get("user_id")).first()
        except TokenError:
            user = None
        data = super().validate(attrs)
        if user is not None:
            data["user"] = UserSerializer(user).data
        return data


@extend_schema(tags=["auth"], summary="Обновление JWT-токенов")
class RefreshView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = CustomTokenRefreshSerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            return Response(
                {"errors": [{"code": "token_not_valid", "message": str(exc), "field": "refresh"}]},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        return Response(serializer.validated_data)


@extend_schema(tags=["auth"], summary="Проверка access-токена")
class VerifyView(TokenVerifyView):
    permission_classes = [AllowAny]
    authentication_classes = []


@extend_schema(tags=["auth"], summary="Смена ПИН-кода")
class ChangePinView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ChangePinSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        log_action(user=user, action="auth.pin_changed", request=request)
        return Response({"detail": _("ПИН-код успешно изменён."), "user": UserSerializer(user).data})


@extend_schema(tags=["auth"], summary="Запрос на восстановление ПИН-кода")
class PinRecoveryRequestCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PinRecoveryRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()
        log_action(user=request.user, action="auth.pin_recovery_requested", target=instance, request=request)
        return Response(PinRecoveryRequestSerializer(instance).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=["auth"], summary="Список запросов на восстановление ПИН (персонал)")
class PinRecoveryRequestListView(generics.ListAPIView):
    permission_classes = [IsStaffRole]
    serializer_class = PinRecoveryRequestSerializer
    filterset_fields = ["status"]
    ordering = ["-created_at"]

    def get_queryset(self):
        return PinRecoveryRequest.objects.select_related("user", "decided_by")


@extend_schema(tags=["auth"], summary="Решение по запросу на восстановление ПИН (админ)")
class PinRecoveryRequestDecideView(APIView):
    permission_classes = [IsAdminRole]

    def post(self, request, pk):
        try:
            instance = PinRecoveryRequest.objects.select_related("user").get(pk=pk)
        except PinRecoveryRequest.DoesNotExist:
            return Response(
                {"errors": [{"code": "not_found", "message": _("Запрос не найден."), "field": None}]},
                status=status.HTTP_404_NOT_FOUND,
            )
        if instance.status != PinRecoveryRequest.Status.PENDING:
            return Response(
                {
                    "errors": [
                        {
                            "code": "already_decided",
                            "message": _("По этому запросу уже принято решение."),
                            "field": None,
                        }
                    ]
                },
                status=status.HTTP_409_CONFLICT,
            )

        serializer = PinRecoveryDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decision = request.data.get("decision", "approve")
        new_pin = serializer.validated_data.get("new_pin")

        if decision == "approve" and not new_pin:
            return Response(
                {
                    "errors": [
                        {
                            "code": "required",
                            "message": _("Укажите новый ПИН-код для одобрения запроса."),
                            "field": "new_pin",
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if decision == "approve":
            instance.user.set_pin(new_pin)
            instance.user.save(
                update_fields=["pin", "pin_set_at", "failed_pin_attempts", "pin_locked_until", "updated_at"]
            )
            instance.status = PinRecoveryRequest.Status.APPROVED
        else:
            instance.status = PinRecoveryRequest.Status.REJECTED

        instance.decided_by = request.user
        instance.decided_at = timezone.now()
        instance.decision_comment = serializer.validated_data.get("decision_comment", "")
        instance.save(
            update_fields=["status", "decided_by", "decided_at", "decision_comment", "updated_at"]
        )
        log_action(user=request.user, action="auth.pin_recovery_decided", target=instance, request=request)
        return Response(PinRecoveryRequestSerializer(instance).data)


@extend_schema(tags=["profile"], summary="Текущий профиль", methods=["GET", "PATCH"])
class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["profile"], summary="Профиль текущего пользователя")
    def get(self, request):
        from apps.trade_points.serializers import TradePointSerializer  # avoids import cycle

        data = UserSerializer(request.user).data
        points = request.user.trade_points.filter(is_active=True).distinct()
        data["trade_points"] = TradePointSerializer(
            points, many=True, context={"request": request}
        ).data
        return Response(data)

    @extend_schema(tags=["profile"], summary="Обновить профиль (имя, язык)")
    def patch(self, request):
        serializer = ProfileUpdateSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserSerializer(request.user).data)


@extend_schema_view(
    list=extend_schema(tags=["staff"], summary="Пользователи (админ)"),
    create=extend_schema(tags=["staff"], summary="Создать пользователя (админ)"),
)
class StaffUserListView(generics.ListCreateAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = UserAdminSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["phone", "username", "first_name", "last_name"]
    ordering_fields = ["created_at", "phone", "role"]
    filterset_fields = ["role", "is_active", "is_blocked"]

    def get_queryset(self):
        return User.objects.all().order_by("-created_at")

    def perform_create(self, serializer):
        user = serializer.save()
        log_action(user=self.request.user, action="user.created", target=user, request=self.request)


@extend_schema_view(
    retrieve=extend_schema(tags=["staff"], summary="Пользователь"),
    update=extend_schema(tags=["staff"], summary="Изменить пользователя"),
    partial_update=extend_schema(tags=["staff"], summary="Изменить пользователя"),
    destroy=extend_schema(tags=["staff"], summary="Деактивировать пользователя"),
)
class StaffUserDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAdminRole]
    serializer_class = UserAdminSerializer
    queryset = User.objects.all()

    def perform_destroy(self, instance: User):
        """Безопасное «удаление» — деактивация, история сохраняется."""
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        log_action(user=self.request.user, action="user.deactivated", target=instance, request=self.request)


@extend_schema(tags=["profile"], summary="Быстрая проверка доступа по роли")
class RoleCheckView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        return Response(
            {
                "role": user.role,
                "is_staff": user.is_staff_role,
                "language": user.language,
                "permissions": {
                    "can_submit_readings": user.can_submit_readings,
                    "can_review_readings": user.is_staff_role,
                    "can_manage_users": user.role in {UserRole.ADMIN, UserRole.SUPER_ADMIN},
                    "can_manage_tariffs": user.role in {UserRole.ADMIN, UserRole.SUPER_ADMIN},
                },
            }
        )
