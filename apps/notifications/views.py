"""Notification endpoints: list, read, devices."""
from django.utils.translation import gettext as _
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import DeviceToken, Notification
from .serializers import DeviceTokenSerializer, NotificationSerializer, UnreadCountSerializer
from .services import mark_all_read, mark_read


def visible_notifications(user):
    qs = Notification.objects.all()
    if getattr(user, "is_staff_role", False) and user.is_superuser:
        return qs
    return qs.filter(user=user)


@extend_schema(tags=["notifications"], summary="Уведомления пользователя")
class NotificationListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = NotificationSerializer
    filterset_fields = ["type", "is_read"]
    ordering_fields = ["created_at"]
    ordering = ["-created_at"]

    def get_queryset(self):
        return visible_notifications(self.request.user)


@extend_schema(tags=["notifications"], summary="Отметить уведомление прочитанным")
class NotificationMarkReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        notification = visible_notifications(request.user).filter(pk=pk).first()
        if notification is None:
            raise NotFound(_("Уведомление не найдено."))
        mark_read(notification)
        return Response(NotificationSerializer(notification).data)


@extend_schema(tags=["notifications"], summary="Отметить все уведомления прочитанными")
class NotificationReadAllView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        count = mark_all_read(request.user)
        return Response({"updated": count})


@extend_schema(tags=["notifications"], summary="Количество непрочитанных", responses=UnreadCountSerializer)
class NotificationUnreadCountView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"unread": request.user.notifications.filter(is_read=False).count()})


@extend_schema_view(
    list=extend_schema(tags=["profile"], summary="Устройства пользователя"),
    create=extend_schema(tags=["profile"], summary="Зарегистрировать push-токен"),
)
class DeviceTokenListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DeviceTokenSerializer

    def get_queryset(self):
        return DeviceToken.objects.filter(user=self.request.user)


@extend_schema(tags=["profile"], summary="Удалить push-токен")
class DeviceTokenDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        device = DeviceToken.objects.filter(pk=pk, user=request.user).first()
        if device is None:
            raise NotFound(_("Устройство не найдено."))
        device.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
