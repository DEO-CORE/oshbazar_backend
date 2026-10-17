from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ReadingsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.readings"
    label = "readings"
    verbose_name = _("Показания электросчётчиков")
