"""Права карты рынков по ролям пользователя.

Права Django в проекте раздаются группами вручную и с ролями не связаны,
поэтому право `trade_points.view_market_map` выдаётся ролям admin и
super_admin этим бэкендом (его можно выдать и группой — через ModelBackend).
Им проверяются ссылки Jazzmin и доступ к /admin/market-map/.
"""
from apps.users.models import UserRole

MARKET_MAP_PERM = "trade_points.view_market_map"
MAP_ROLES = {UserRole.ADMIN, UserRole.SUPER_ADMIN}


def role_allows_market_map(user) -> bool:
    return bool(
        user
        and user.is_authenticated
        and user.is_active
        and user.is_staff
        and (user.is_superuser or getattr(user, "role", None) in MAP_ROLES)
    )


class MarketMapRoleBackend:
    """Не аутентифицирует, только добавляет право на карту по роли."""

    def authenticate(self, request, **credentials):
        return None

    def get_user(self, user_id):
        return None

    def has_perm(self, user_obj, perm, obj=None):
        return perm == MARKET_MAP_PERM and role_allows_market_map(user_obj)

    def has_module_perms(self, user_obj, app_label):
        return False
