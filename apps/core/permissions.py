"""Role based permission classes."""
from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.users.models import UserRole


def user_role(user):
    return getattr(user, "role", None)


class IsRole(BasePermission):
    """Allow access only to users with one of `allowed_roles`."""

    allowed_roles: tuple[str, ...] = ()

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser or user.role == UserRole.SUPER_ADMIN:
            return True
        return user.role in self.allowed_roles


class IsTenant(IsRole):
    allowed_roles = (UserRole.TENANT,)


class IsStaffRole(IsRole):
    """Контролёр, администратор, супер-администратор."""

    allowed_roles = (UserRole.CONTROLLER, UserRole.ADMIN, UserRole.SUPER_ADMIN)


class IsAdminRole(IsRole):
    allowed_roles = (UserRole.ADMIN, UserRole.SUPER_ADMIN)


class IsSuperAdmin(IsRole):
    allowed_roles = (UserRole.SUPER_ADMIN,)


class IsStaffOrReadOnly(IsStaffRole):
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return super().has_permission(request, view)


class ReadOnly(BasePermission):
    def has_permission(self, request, view):
        return request.method in SAFE_METHODS
