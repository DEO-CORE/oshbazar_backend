from django.urls import path

from .views import FeaturesView, VersionCheckView

app_name = "app_releases"

urlpatterns = [
    path("app/version-check/", VersionCheckView.as_view(), name="version-check"),
    path("app/features/", FeaturesView.as_view(), name="features"),
]
