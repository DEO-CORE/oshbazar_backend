"""URL routes for the 1C integration."""
from django.urls import path

from .views import ExternalObjectListView, OneCStatusView, OneCSyncView, SyncRunListView

app_name = "integrations"

urlpatterns = [
    path("integrations/1c/status/", OneCStatusView.as_view(), name="status"),
    path("integrations/1c/sync/", OneCSyncView.as_view(), name="sync"),
    path("integrations/1c/runs/", SyncRunListView.as_view(), name="runs"),
    path("integrations/1c/objects/", ExternalObjectListView.as_view(), name="objects"),
]
