from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .models import MeterReading, ReadingPhoto, ReadingStatus


@admin.register(ReadingPhoto)
class ReadingPhotoAdmin(admin.ModelAdmin):
    list_display = ("trade_point", "meter", "original_value", "uploaded_by", "uploaded_at", "preview")
    list_filter = ("uploaded_at",)
    search_fields = ("trade_point__code", "meter__serial_number", "uploaded_by__phone")
    readonly_fields = ("uploaded_at", "size_bytes", "content_type", "preview")

    @admin.display(description=_("Превью"))
    def preview(self, obj: ReadingPhoto):
        if not obj.file:
            return "—"
        return format_html('<a href="{}" target="_blank"><img src="{}" style="height:80px"/></a>',
                           obj.file.url, obj.file.url)


@admin.register(MeterReading)
class MeterReadingAdmin(admin.ModelAdmin):
    list_display = ("trade_point", "period", "meter", "previous_value", "value", "consumption_display",
                    "status", "submitted_by", "submitted_at")
    list_filter = ("status", "source", "period")
    search_fields = ("trade_point__code", "trade_point__name", "meter__serial_number",
                     "submitted_by__phone")
    date_hierarchy = "submitted_at"
    ordering = ("-submitted_at",)
    readonly_fields = ("created_at", "updated_at", "submitted_at", "reviewed_at")
    actions = ["approve_readings", "reject_readings"]

    @admin.display(description=_("Потребление"))
    def consumption_display(self, obj: MeterReading):
        return f"{obj.consumption} кВт⋅ч"

    @admin.action(description=_("Принять показания и сформировать начисления"))
    def approve_readings(self, request, queryset):
        from .services import review_reading

        done = 0
        for reading in queryset.filter(status=ReadingStatus.PENDING):
            review_reading(reading, action="approve", actor=request.user)
            done += 1
        self.message_user(request, f"Принято показаний: {done}")

    @admin.action(description=_("Отклонить показания"))
    def reject_readings(self, request, queryset):
        from .services import review_reading

        done = 0
        for reading in queryset.filter(status=ReadingStatus.PENDING):
            review_reading(reading, action="reject", actor=request.user, reason="Отклонено администратором")
            done += 1
        self.message_user(request, f"Отклонено показаний: {done}")
