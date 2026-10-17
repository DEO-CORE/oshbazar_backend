"""URL routes: auth, profile, staff user management."""
from django.urls import path

from .views import (
    ChangePinView,
    LoginView,
    LogoutView,
    MeView,
    PinRecoveryRequestCreateView,
    PinRecoveryRequestDecideView,
    PinRecoveryRequestListView,
    RefreshView,
    RoleCheckView,
    StaffUserDetailView,
    StaffUserListView,
    VerifyView,
)

app_name = "users"

urlpatterns = [
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/refresh/", RefreshView.as_view(), name="refresh"),
    path("auth/verify/", VerifyView.as_view(), name="verify"),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
    path("auth/change-pin/", ChangePinView.as_view(), name="change-pin"),
    path("auth/pin-recovery/", PinRecoveryRequestCreateView.as_view(), name="pin-recovery"),
    path("auth/pin-recovery/requests/", PinRecoveryRequestListView.as_view(), name="pin-recovery-list"),
    path(
        "auth/pin-recovery/requests/<uuid:pk>/decide/",
        PinRecoveryRequestDecideView.as_view(),
        name="pin-recovery-decide",
    ),
    path("me/", MeView.as_view(), name="me"),
    path("me/role/", RoleCheckView.as_view(), name="me-role"),
    path("staff/users/", StaffUserListView.as_view(), name="staff-users"),
    path("staff/users/<uuid:pk>/", StaffUserDetailView.as_view(), name="staff-user"),
]
