"""URL routes for notifications and devices."""
from django.urls import path

from .views import (
    DeviceTokenDeleteView,
    DeviceTokenListCreateView,
    NotificationListView,
    NotificationMarkReadView,
    NotificationReadAllView,
    NotificationUnreadCountView,
)

app_name = "notifications"

urlpatterns = [
    path("notifications/", NotificationListView.as_view(), name="list"),
    path("notifications/unread-count/", NotificationUnreadCountView.as_view(), name="unread-count"),
    path("notifications/read-all/", NotificationReadAllView.as_view(), name="read-all"),
    path("notifications/<uuid:pk>/read/", NotificationMarkReadView.as_view(), name="mark-read"),
    path("devices/", DeviceTokenListCreateView.as_view(), name="device-list"),
    path("devices/<uuid:pk>/", DeviceTokenDeleteView.as_view(), name="device-detail"),
]
