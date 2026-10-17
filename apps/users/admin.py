from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import PinRecoveryRequest, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = (
        "phone",
        "username",
        "full_name",
        "role",
        "language",
        "is_active",
        "is_blocked",
        "created_at",
    )
    list_filter = ("role", "language", "is_active", "is_blocked", "is_staff")
    search_fields = ("phone", "username", "first_name", "last_name", "email")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    fieldsets = (
        (None, {"fields": ("phone", "username", "password")}),
        (_("Личные данные"), {"fields": ("first_name", "last_name", "email")}),
        (_("Роли и права"), {"fields": ("role", "language", "is_active", "is_staff", "is_superuser", "groups")}),
        (_("ПИН-код"), {"fields": ("pin", "pin_set_at", "failed_pin_attempts", "pin_locked_until", "is_blocked")}),
        (_("Даты"), {"fields": ("last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("phone", "username", "role", "language", "password1", "password2"),
            },
        ),
    )
    readonly_fields = ("created_at", "updated_at", "pin_set_at", "last_login")
    filter_horizontal = ("groups", "user_permissions")


@admin.register(PinRecoveryRequest)
class PinRecoveryRequestAdmin(admin.ModelAdmin):
    list_display = ("user", "status", "created_at", "decided_by", "decided_at")
    list_filter = ("status",)
    search_fields = ("user__phone", "user__username", "user__first_name", "user__last_name")
    readonly_fields = ("created_at", "updated_at", "decided_at", "decided_by")
    ordering = ("-created_at",)
