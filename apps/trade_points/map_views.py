"""Интерактивная карта рынков в админке (/admin/market-map/)."""
from functools import wraps

from django.contrib import admin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from .backends import MARKET_MAP_PERM, role_allows_market_map
from .market_map import STATUS_LABELS, build_map_payload, point_detail
from .models import Market, TradePoint


def can_view_market_map(user) -> bool:
    """Персонал админки с правом trade_points.view_market_map.

    Право есть у ролей admin / super_admin (MarketMapRoleBackend) и у тех,
    кому его выдали группой; у контролёров его нет.
    """
    if not (user and user.is_authenticated and user.is_active and user.is_staff):
        return False
    return role_allows_market_map(user) or user.has_perm(MARKET_MAP_PERM)


def map_access(json: bool = False):
    def decorator(view):
        @wraps(view)
        def inner(request, *args, **kwargs):
            if not can_view_market_map(request.user):
                if json:
                    return JsonResponse({"errors": [{"code": "forbidden", "message": "Нет доступа"}]}, status=403)
                response = render(
                    request,
                    "admin/market_map/forbidden.html",
                    {**admin.site.each_context(request), "title": "Карта рынков"},
                    status=403,
                )
                return response
            return view(request, *args, **kwargs)

        # admin_view: вход в админку, never_cache, проверка is_staff.
        return admin.site.admin_view(inner)

    return decorator


@map_access()
def map_page(request):
    context = {
        **admin.site.each_context(request),
        "title": "Карта рынков",
        "markets": Market.objects.filter(is_active=True).order_by("sort_order", "name"),
        "statuses": STATUS_LABELS,
        "data_url": reverse("market_map:data"),
        "tradepoint_changelist": reverse("admin:trade_points_tradepoint_changelist"),
        "market_changelist": reverse("admin:trade_points_market_changelist"),
    }
    return render(request, "admin/market_map/index.html", context)


@map_access(json=True)
def map_data(request):
    response = JsonResponse(build_map_payload(), json_dumps_params={"ensure_ascii": False})
    response["Cache-Control"] = "no-store"
    return response


@map_access(json=True)
def map_point(request, pk):
    point = get_object_or_404(TradePoint.objects.select_related("market"), pk=pk)
    return JsonResponse(point_detail(point), json_dumps_params={"ensure_ascii": False})
