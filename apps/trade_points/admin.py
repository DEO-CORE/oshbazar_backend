from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import Meter, TradePoint, TradePointAccess


class MeterInline(admin.TabularInline):
    model = Meter
    extra = 0
    fields = ("serial_number", "model_name", "initial_reading", "installed_at", "is_primary", "is_active")
    readonly_fields = ("created_at",)


class AccessInline(admin.TabularInline):
    model = TradePointAccess
    extra = 0
    fk_name = "trade_point"
    fields = ("user", "access_role", "is_active", "granted_by")
    autocomplete_fields = ("user",)


@admin.register(TradePoint)
class TradePointAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "block", "place_number", "is_active", "has_qr", "created_at")
    list_filter = ("is_active", "block")
    search_fields = ("code", "name", "place_number", "address", "external_id_1c")
    ordering = ("block", "place_number", "code")
    inlines = [MeterInline, AccessInline]
    readonly_fields = ("created_at", "updated_at", "qr_payload")
    actions = ["deactivate"]

    @admin.display(boolean=True, description=_("QR"))
    def has_qr(self, obj: TradePoint) -> bool:
        return bool(obj.qr_data)

    @admin.action(description=_("Деактивировать выбранные точки"))
    def deactivate(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"Деактивировано точек: {updated}")


@admin.register(Meter)
class MeterAdmin(admin.ModelAdmin):
    list_display = (
        "serial_number",
        "trade_point",
        "model_name",
        "initial_reading",
        "is_primary",
        "is_active",
    )
    list_filter = ("is_active", "is_primary")
    search_fields = ("serial_number", "model_name", "trade_point__code", "trade_point__name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(TradePointAccess)
class TradePointAccessAdmin(admin.ModelAdmin):
    list_display = ("user", "trade_point", "access_role", "is_active", "granted_by", "created_at")
    list_filter = ("access_role", "is_active")
    search_fields = ("user__phone", "user__username", "trade_point__code", "trade_point__name")
    autocomplete_fields = ("user", "trade_point")
    readonly_fields = ("created_at", "updated_at")
