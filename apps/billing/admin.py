from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import BillingPeriod, Charge, ChargeStatus, LedgerEntry, Tariff


@admin.register(BillingPeriod)
class BillingPeriodAdmin(admin.ModelAdmin):
    list_display = ("label", "title", "starts_on", "ends_on", "is_open", "closed_at")
    list_filter = ("is_open", "year", "month")
    readonly_fields = ("closed_at", "created_at", "updated_at")
    ordering = ("-year", "-month")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Tariff)
class TariffAdmin(admin.ModelAdmin):
    list_display = ("name", "price", "currency", "valid_from", "valid_to", "is_active", "created_by")
    list_filter = ("is_active", "currency")
    ordering = ("-valid_from",)
    readonly_fields = ("created_at", "updated_at", "created_by")

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Charge)
class ChargeAdmin(admin.ModelAdmin):
    list_display = ("period", "trade_point", "owner", "consumption", "tariff_price", "amount",
                    "paid_amount", "status", "due_date")
    list_filter = ("status", "is_manual", "period")
    search_fields = ("trade_point__code", "trade_point__name", "owner__phone", "meter__serial_number")
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at", "paid_at", "source_reading_id")
    actions = ["recalculate_selected"]
    exclude = ()

    @admin.action(description=_("Пересчитать выбранные начисления по тарифу"))
    def recalculate_selected(self, request, queryset):
        from .services import recalculate_charge

        done, skipped = 0, 0
        for charge in queryset:
            try:
                recalculate_charge(charge, actor=request.user)
                done += 1
            except ValueError:
                skipped += 1
        self.message_user(request, f"Пересчитано: {done}, пропущено (есть оплата): {skipped}")

    def has_delete_permission(self, request, obj=None):
        # Исторические финансовые данные не удаляются (ТЗ, п. 24).
        return False


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "kind", "source", "amount", "balance_after", "trade_point")
    list_filter = ("kind", "source")
    search_fields = ("user__phone", "note", "charge__trade_point__code")
    ordering = ("-created_at",)
    readonly_fields = [f.name for f in LedgerEntry._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
