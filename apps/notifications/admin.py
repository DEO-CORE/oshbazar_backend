from django.contrib import admin

from .models import DeviceToken, Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "type", "title", "is_read", "push_sent")
    list_filter = ("type", "is_read", "push_sent")
    search_fields = ("user__phone", "title", "body")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at", "read_at", "push_sent", "push_error")


@admin.register(DeviceToken)
class DeviceTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "platform", "locale", "is_active", "last_seen_at")
    list_filter = ("platform", "is_active", "locale")
    search_fields = ("user__phone", "token")
    readonly_fields = ("created_at", "updated_at", "last_seen_at")
