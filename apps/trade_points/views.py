"""Trade point endpoints: list, detail, QR-code, scan, summary."""
import segno
from django.http import HttpResponse
from django_filters.rest_framework import DjangoFilterBackend
from django.utils.translation import gettext as _
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiExample
from rest_framework import filters, generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.audit import log_action
from apps.core.permissions import IsAdminRole, IsStaffOrReadOnly, IsStaffRole

from .models import Meter, TradePoint
from .serializers import (
    MeterSerializer,
    QRCodeSerializer,
    ScanResultSerializer,
    TradePointCreateSerializer,
    TradePointSerializer,
)
from .services import accessible_points_queryset, accessible_trade_points, get_trade_point_for_user


def _current_period_label() -> str:
    from apps.billing.services import current_period

    period = current_period()
    return period.label if period else ""


@extend_schema_view(
    list=extend_schema(
        tags=["trade-points"],
        summary="Мои торговые точки",
        description=(
            "Арендатор получает список закреплённых за ним точек; "
            "контролёр/администратор — все точки с фильтрами и поиском."
        ),
    ),
    create=extend_schema(tags=["staff"], summary="Создать торговую точку (админ)"),
)
class TradePointListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsStaffOrReadOnly]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter, DjangoFilterBackend]
    search_fields = ["code", "name", "block", "place_number", "address"]
    ordering_fields = ["code", "name", "block", "created_at"]
    filterset_fields = ["is_active", "block"]
    ordering = ["block", "place_number", "code"]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return TradePointCreateSerializer
        return TradePointSerializer

    def get_queryset(self):
        return accessible_points_queryset(self.request.user)

    def perform_create(self, serializer):
        point = serializer.save()
        log_action(user=self.request.user, action="trade_point.created", target=point, request=self.request)


@extend_schema_view(
    retrieve=extend_schema(tags=["trade-points"], summary="Торговая точка"),
    update=extend_schema(tags=["staff"], summary="Изменить торговую точку"),
    partial_update=extend_schema(tags=["staff"], summary="Изменить торговую точку"),
    destroy=extend_schema(tags=["staff"], summary="Деактивировать торговую точку"),
)
class TradePointDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsStaffOrReadOnly]
    queryset = TradePoint.objects.all()

    def get_serializer_class(self):
        if self.request.method in ("PUT", "PATCH"):
            return TradePointCreateSerializer
        return TradePointSerializer

    def get_object(self):
        return get_trade_point_for_user(self.request.user, self.kwargs["pk"])

    def perform_destroy(self, instance: TradePoint):
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])
        log_action(user=self.request.user, action="trade_point.deactivated", target=instance, request=self.request)


@extend_schema(
    tags=["trade-points"],
    summary="QR-код торговой точки",
    description="Возвращает PNG (по умолчанию) или SVG с QR-кодом, содержащим `OSHBAZAR:TP:<код>`.",
    responses={(200, "image/png"): bytes, (200, "image/svg+xml"): bytes},
    examples=[OpenApiExample("PNG", value="binary image data", media_type="image/png")],
)
class TradePointQRView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        point = get_trade_point_for_user(request.user, pk)
        fmt = (request.query_params.get("format") or "png").lower()
        qr = segno.make(point.qr_data, error="m")
        if fmt == "svg":
            buffer = qr.svg_data_uri() if request.query_params.get("inline") else qr.svg_inline()
            return HttpResponse(buffer, content_type="image/svg+xml")
        import io

        stream = io.BytesIO()
        qr.save(stream, kind="png", scale=6, border=2)
        response = HttpResponse(stream.getvalue(), content_type="image/png")
        response["Content-Disposition"] = f'inline; filename="qr-{point.code}.png"'
        return response


@extend_schema(
    tags=["trade-points"],
    summary="Информация по QR-коду (сканирование)",
    description="Возвращает данные торговой точки по коду из QR-кода.",
    responses=ScanResultSerializer,
)
class TradePointScanView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, code):
        point = TradePoint.objects.filter(code=code).first()
        if point is None:
            return Response(
                {
                    "errors": [
                        {"code": "not_found", "message": _("Торговая точка с таким кодом не найдена."), "field": "code"}
                    ]
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        allowed = accessible_trade_points(request.user).filter(pk=point.pk).exists()
        payload = {
            "found": True,
            "is_accessible": allowed,
            "trade_point": TradePointSerializer(point, context={"request": request}).data,
        }
        if allowed:
            payload["meters"] = MeterSerializer(
                point.meters.filter(is_active=True), many=True
            ).data
        else:
            # Не раскрываем чужие данные: только идентификация точки.
            payload["trade_point"] = {
                "id": str(point.id),
                "code": point.code,
                "name": point.name,
            }
        return Response(payload)


@extend_schema(
    tags=["trade-points"],
    summary="Сводка по торговой точке",
    description="Последние показания, текущее начисление и задолженность по точке.",
)
class TradePointSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from apps.billing.services import point_summary

        point = get_trade_point_for_user(request.user, pk)
        return Response(point_summary(point, request.user))
