"""Access helpers for trade points."""
from django.db.models import Count, Q, QuerySet
from django.http import Http404
from django.utils.translation import gettext as _

from .models import TradePoint


def accessible_trade_points(user) -> QuerySet[TradePoint]:
    """Точки, доступные пользователю; персонал видит все точки."""
    qs = TradePoint.objects.all()
    if user is None or not user.is_authenticated:
        return qs.none()
    if getattr(user, "is_staff_role", False):
        return qs
    return qs.filter(user_accesses__user=user, user_accesses__is_active=True, is_active=True)


def accessible_points_queryset(user) -> QuerySet[TradePoint]:
    return (
        accessible_trade_points(user)
        .select_related()
        .prefetch_related("meters", "user_accesses")
        .distinct()
    )


def get_trade_point_for_user(user, pk) -> TradePoint:
    """Object level permission: арендатор не видит чужие точки."""
    point = TradePoint.objects.filter(pk=pk).first()
    if point is None:
        raise Http404(_("Торговая точка не найдена."))
    allowed = accessible_trade_points(user).filter(pk=point.pk).exists()
    if not allowed:
        # Do not reveal whether the point exists for another user.
        raise Http404(_("Торговая точка не найдена."))
    return point


def point_stats(point: TradePoint) -> dict:
    return {
        "active_meters": point.meters.filter(is_active=True).count(),
        "tenants": point.user_accesses.filter(is_active=True).count(),
        "readings_total": point.readings.count(),
    }


def annotate_tenant_count(qs: QuerySet[TradePoint]) -> QuerySet[TradePoint]:
    return qs.annotate(
        tenant_count=Count(
            "user_accesses",
            filter=Q(user_accesses__is_active=True),
            distinct=True,
        )
    )
