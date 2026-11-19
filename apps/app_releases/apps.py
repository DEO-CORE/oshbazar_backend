from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AppReleasesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.app_releases"
    label = "app_releases"
    verbose_name = _("Выпуск изменений: релизы и фича-флаги")
