"""URL routes for meter readings."""
from django.urls import path

from .views import (
    ReadingDetailView,
    ReadingListCreateView,
    ReadingPhotoUploadView,
    ReadingPreviewView,
    ReadingReviewView,
    ReadingStatsView,
)

app_name = "readings"

urlpatterns = [
    path("readings/", ReadingListCreateView.as_view(), name="list"),
    path("readings/preview/", ReadingPreviewView.as_view(), name="preview"),
    path("readings/stats/", ReadingStatsView.as_view(), name="stats"),
    path("readings/<uuid:pk>/", ReadingDetailView.as_view(), name="detail"),
    path("readings/<uuid:pk>/photo/", ReadingPhotoUploadView.as_view(), name="photo"),
    path("readings/<uuid:pk>/review/", ReadingReviewView.as_view(), name="review"),
]
