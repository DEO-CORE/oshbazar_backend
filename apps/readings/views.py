"""Endpoints for submitting, previewing and reviewing meter readings."""
from django.db.models import Count, Q
from django.http import FileResponse
from django.utils.translation import gettext as _
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.services import current_period
from apps.core.audit import log_action
from apps.core.permissions import IsStaffRole
from apps.trade_points.services import accessible_trade_points

from .models import MeterReading, ReadingStatus
from .serializers import (
    MeterReadingSerializer,
    ReadingPhotoSerializer,
    ReadingPreviewSerializer,
    ReadingReviewSerializer,
    ReadingStatsSerializer,
    ReadingSubmitSerializer,
)
from .services import attach_photo, estimate, review_reading


def reading_visible_to(user, reading: MeterReading) -> bool:
    if getattr(user, "is_staff_role", False):
        return True
    if reading.submitted_by_id == user.id:
        return True
    return accessible_trade_points(user).filter(pk=reading.trade_point_id).exists()


def visible_readings(user):
    qs = MeterReading.objects.select_related(
        "period", "trade_point", "meter", "photo", "submitted_by", "reviewed_by"
    )
    if getattr(user, "is_staff_role", False):
        return qs
    return qs.filter(
        Q(submitted_by=user) | Q(trade_point__in=accessible_trade_points(user))
    ).distinct()


@extend_schema_view(
    list=extend_schema(
        tags=["readings"],
        summary="Показания",
        description="Арендатор видит показания своих точек, персонал — все с фильтрами.",
    ),
    create=extend_schema(
        tags=["readings"],
        summary="Передать показание",
        request={"multipart/form-data": ReadingSubmitSerializer},
        responses={201: MeterReadingSerializer},
    ),
)
class ReadingListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filterset_fields = ["trade_point", "status", "period", "meter"]
    ordering_fields = ["submitted_at", "value"]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ReadingSubmitSerializer
        return MeterReadingSerializer

    def get_queryset(self):
        return visible_readings(self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        reading = serializer.save()
        log_action(user=request.user, action="readings.submitted", target=reading, request=request,
                   extra={"value": str(reading.value)})
        return Response(
            MeterReadingSerializer(reading, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


@extend_schema(tags=["readings"], summary="Показание")
class ReadingDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = MeterReadingSerializer

    def get_object(self):
        reading = (
            MeterReading.objects.select_related(
                "period", "trade_point", "meter", "photo", "submitted_by", "reviewed_by"
            )
            .filter(pk=self.kwargs["pk"])
            .first()
        )
        if reading is None or not reading_visible_to(self.request.user, reading):
            raise NotFound(_("Показание не найдено."))
        return reading


@extend_schema(
    tags=["readings"],
    summary="Предварительный расчёт показания",
    description="Возвращает предыдущее показание, расход, тариф и сумму к оплате до отправки.",
    request=ReadingPreviewSerializer,
    responses={200: None},
)
class ReadingPreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ReadingPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        meter = serializer.validated_data["meter"]
        accessible = accessible_trade_points(request.user).filter(pk=meter.trade_point_id).exists()
        if not accessible:
            raise PermissionDenied(_("У вас нет доступа к этой торговой точке."))
        data = estimate(meter, serializer.validated_data["value"])
        return Response(data)


@extend_schema(
    tags=["readings"],
    summary="Приложить фотографию счётчика",
    request={"multipart/form-data": ReadingPhotoSerializer},
    responses={200: MeterReadingSerializer},
)
class ReadingPhotoUploadView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk):
        reading = MeterReading.objects.filter(pk=pk).first()
        if reading is None or not reading_visible_to(request.user, reading):
            raise NotFound(_("Показание не найдено."))
        if reading.submitted_by_id != request.user.id and not request.user.is_staff_role:
            raise PermissionDenied(_("Нельзя изменять чужое показание."))
        photo_file = request.FILES.get("photo") or request.FILES.get("file")
        if photo_file is None:
            return Response(
                {"errors": [{"code": "required", "message": _("Приложите файл фотографии."), "field": "photo"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        attach_photo(reading, photo_file, request.user)
        reading.refresh_from_db()
        return Response(MeterReadingSerializer(reading, context={"request": request}).data)


@extend_schema(
    tags=["staff"],
    summary="Проверить показание (контролёр / админ)",
    request=ReadingReviewSerializer,
    responses={200: MeterReadingSerializer},
)
class ReadingReviewView(APIView):
    permission_classes = [IsStaffRole]

    def post(self, request, pk):
        reading = MeterReading.objects.select_related("trade_point", "period", "submitted_by").filter(pk=pk).first()
        if reading is None:
            raise NotFound(_("Показание не найдено."))
        serializer = ReadingReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        review_reading(
            reading,
            action=serializer.validated_data["action"],
            actor=request.user,
            reason=serializer.validated_data.get("reason", ""),
        )
        log_action(
            user=request.user,
            action=f"readings.{serializer.validated_data['action']}d",
            target=reading,
            request=request,
        )
        return Response(MeterReadingSerializer(reading, context={"request": request}).data)


@extend_schema(
    tags=["staff"],
    summary="Статистика по показаниям за период",
    responses=ReadingStatsSerializer,
)
class ReadingStatsView(APIView):
    permission_classes = [IsStaffRole]

    def get(self, request):
        period = current_period()
        aggregations = visible_readings(request.user).filter(period=period).aggregate(
            total=Count("id"),
            pending=Count("id", filter=Q(status=ReadingStatus.PENDING)),
            approved=Count("id", filter=Q(status=ReadingStatus.APPROVED)),
            rejected=Count("id", filter=Q(status=ReadingStatus.REJECTED)),
        )
        return Response({**aggregations, "period": period.label})


class ReadingPhotoFileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        reading = visible_readings(request.user).filter(photo_id=pk).select_related("photo").first()
        if reading is None or not reading.photo.file:
            raise NotFound(_("Фотография не найдена."))
        try:
            response = FileResponse(reading.photo.file.open("rb"),
                                    content_type=reading.photo.content_type or "application/octet-stream")
        except FileNotFoundError:
            raise NotFound(_("Фотография не найдена."))
        response["Cache-Control"] = "private, no-store"
        return response
