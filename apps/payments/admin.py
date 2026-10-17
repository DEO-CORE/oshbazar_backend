from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import BankWebhookLog, Payment, PaymentAllocation, Receipt


class PaymentAllocationInline(admin.TabularInline):
    model = PaymentAllocation
    extra = 0
    readonly_fields = ("charge", "amount", "created_at")
    can_delete = False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "amount", "currency", "status", "provider",
                    "provider_payment_id", "trade_point")
    list_filter = ("status", "provider", "currency")
    search_fields = ("user__phone", "user__last_name", "provider_payment_id", "id")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at", "initiated_at", "completed_at", "raw_payload",
                       "redirect_url", "provider_payment_id", "idempotency_key")
    inlines = [PaymentAllocationInline]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = ("number", "payment", "issued_at")
    search_fields = ("number", "payment__user__phone", "payment__provider_payment_id")
    readonly_fields = ("number", "payment", "data", "issued_at", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BankWebhookLog)
class BankWebhookLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "provider", "external_id", "signature_valid", "processed")
    list_filter = ("provider", "signature_valid", "processed")
    search_fields = ("external_id", "error")
    ordering = ("-created_at",)
    readonly_fields = [f.name for f in BankWebhookLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
