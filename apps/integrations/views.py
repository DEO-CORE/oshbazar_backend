"""1C integration endpoints (super-admin only)."""
from django.utils.translation import gettext as _
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.audit import log_action
from apps.core.permissions import IsSuperAdmin

from .models import ExternalObject, SyncRun
from .serializers import ExternalObjectSerializer, SyncRequestSerializer, SyncRunSerializer
from .services import integration_status, run_sync


@extend_schema(tags=["integrations"], summary="Статус интеграции с 1С")
class OneCStatusView(APIView):
    permission_classes = [IsSuperAdmin]

    def get(self, request):
        return Response(integration_status())


@extend_schema(
    tags=["integrations"],
    summary="Запустить синхронизацию с 1С",
    request=SyncRequestSerializer,
    responses={200: SyncRunSerializer},
)
class OneCSyncView(APIView):
    permission_classes = [IsSuperAdmin]

    def post(self, request):
        serializer = SyncRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        run = run_sync(data["entity"], direction=data["direction"], dry_run=data["dry_run"])
        log_action(user=request.user, action="integrations.1c_sync", target=run, request=request)
        return Response(SyncRunSerializer(run).data)


@extend_schema(tags=["integrations"], summary="Журнал запусков синхронизации")
class SyncRunListView(generics.ListAPIView):
    permission_classes = [IsSuperAdmin]
    serializer_class = SyncRunSerializer
    filterset_fields = ["entity", "status", "direction"]
    ordering = ["-started_at"]

    def get_queryset(self):
        return SyncRun.objects.all()


@extend_schema(tags=["integrations"], summary="Связи объектов с 1С")
class ExternalObjectListView(generics.ListAPIView):
    permission_classes = [IsSuperAdmin]
    serializer_class = ExternalObjectSerializer
    filterset_fields = ["entity", "external_id"]
    search_fields = ["external_id", "payload"]
    ordering = ["-synced_at"]

    def get_queryset(self):
        return ExternalObject.objects.all()
