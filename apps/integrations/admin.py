from django.contrib import admin

from .models import ExternalObject, SyncRun


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ("started_at", "entity", "direction", "status", "finished_at")
    list_filter = ("entity", "direction", "status")
    ordering = ("-started_at",)
    readonly_fields = [f.name for f in SyncRun._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(ExternalObject)
class ExternalObjectAdmin(admin.ModelAdmin):
    list_display = ("entity", "external_id", "internal_id", "synced_at")
    list_filter = ("entity",)
    search_fields = ("external_id",)
    readonly_fields = ("created_at", "updated_at", "synced_at")
