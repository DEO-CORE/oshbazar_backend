from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class TradePointsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.trade_points"
    label = "trade_points"
    verbose_name = _("Торговые точки и счётчики")
