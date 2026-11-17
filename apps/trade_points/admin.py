import csv
import io
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _

from apps.users.models import User
from apps.users.services import normalize_inn

from .models import AccessRole, Meter, TradePoint, TradePointAccess


def suggest_code(block: str, place_number: str) -> str:
    """Код точки из корпуса и места: «А» + «12» → «А-12»."""
    parts = [p.strip().replace(" ", "") for p in (block, place_number) if p and p.strip()]
    return "-".join(parts).upper()[:32]


class MeterInlineFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        primary = [
            form
            for form in self.forms
            if getattr(form, "cleaned_data", None)
            and not form.cleaned_data.get("DELETE")
            and form.cleaned_data.get("is_primary")
            and form.cleaned_data.get("is_active")
        ]
        if len(primary) > 1:
            raise ValidationError(_("Основным может быть только один активный счётчик."))


class MeterInline(admin.TabularInline):
    model = Meter
    formset = MeterInlineFormSet
    fields = ("serial_number", "model_name", "initial_reading", "installed_at", "is_primary", "is_active")

    def get_extra(self, request, obj=None, **kwargs):
        # При создании точки сразу показываем строку для счётчика.
        return 1 if obj is None else 0


class AccessInline(admin.TabularInline):
    model = TradePointAccess
    fk_name = "trade_point"
    fields = ("user", "access_role", "is_active")
    autocomplete_fields = ("user",)
    verbose_name = _("Арендатор / сотрудник")
    verbose_name_plural = _("Кто пользуется точкой (поиск по ИНН, телефону или ФИО)")

    def get_extra(self, request, obj=None, **kwargs):
        return 1 if obj is None else 0


class TradePointForm(forms.ModelForm):
    class Meta:
        model = TradePoint
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "code" in self.fields:
            self.fields["code"].required = False
            self.fields["code"].help_text = _(
                "Можно оставить пустым — код сформируется из корпуса и места, например «А-12»."
            )

    def clean(self):
        data = super().clean()
        if not data.get("code"):
            code = suggest_code(data.get("block", ""), data.get("place_number", ""))
            if not code:
                raise ValidationError(
                    {"code": _("Укажите код точки или заполните корпус и номер места.")}
                )
            if TradePoint.objects.filter(code=code).exclude(pk=self.instance.pk).exists():
                raise ValidationError(
                    {"code": _("Точка с кодом «%(code)s» уже есть. Укажите код вручную.") % {"code": code}}
                )
            data["code"] = code
            self.instance.code = code
        return data


class ImportForm(forms.Form):
    file = forms.FileField(
        label=_("Файл CSV"),
        help_text=_("Excel → «Сохранить как» → CSV UTF-8. Первая строка — заголовки столбцов."),
    )


IMPORT_COLUMNS = (
    "code",
    "name",
    "block",
    "place_number",
    "address",
    "area_m2",
    "meter_serial",
    "meter_model",
    "initial_reading",
    "tenant_inn",
)


def _decimal(value: str, field: str) -> Decimal | None:
    value = (value or "").strip().replace(" ", "").replace(",", ".")
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field}: «{value}» — не число") from exc


def import_trade_points(text: str, granted_by: User | None) -> tuple[int, int, list[str]]:
    """Создаёт или обновляет точки из CSV. Возвращает (создано, обновлено, ошибки).

    Каждая строка — отдельная транзакция: ошибка в одной не отменяет остальные.
    """
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    reader.fieldnames = [(h or "").strip().lower() for h in reader.fieldnames or []]
    if "name" not in reader.fieldnames:
        return 0, 0, [f"Нет столбца «name». Ожидаются столбцы: {', '.join(IMPORT_COLUMNS)}"]

    created = updated = 0
    errors: list[str] = []
    for line, row in enumerate(reader, start=2):
        row = {k: (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue
        try:
            with transaction.atomic():
                code = row.get("code") or suggest_code(row.get("block", ""), row.get("place_number", ""))
                if not code:
                    raise ValueError("нет кода и нет корпуса/места")
                if not row.get("name"):
                    raise ValueError("не указано название")
                point, is_new = TradePoint.objects.update_or_create(
                    code=code,
                    defaults={
                        k: v
                        for k, v in {
                            "name": row.get("name"),
                            "block": row.get("block", ""),
                            "place_number": row.get("place_number", ""),
                            "address": row.get("address", ""),
                            "area_m2": _decimal(row.get("area_m2", ""), "area_m2"),
                        }.items()
                        if v not in (None, "") or k == "name"
                    },
                )
                if serial := row.get("meter_serial"):
                    meter = Meter.objects.filter(serial_number=serial).first()
                    if meter and meter.trade_point_id != point.pk:
                        raise ValueError(f"счётчик {serial} уже привязан к точке {meter.trade_point.code}")
                    if meter is None:
                        has_primary = point.meters.filter(is_primary=True, is_active=True).exists()
                        Meter.objects.create(
                            trade_point=point,
                            serial_number=serial,
                            model_name=row.get("meter_model", ""),
                            initial_reading=_decimal(row.get("initial_reading", ""), "initial_reading")
                            or 0,
                            is_primary=not has_primary,
                        )
                if inn := normalize_inn(row.get("tenant_inn", "")):
                    user = User.objects.filter(inn=inn).first()
                    if user is None:
                        raise ValueError(f"пользователь с ИНН {inn} не найден — сначала создайте его")
                    TradePointAccess.objects.get_or_create(
                        user=user,
                        trade_point=point,
                        defaults={"access_role": AccessRole.TENANT, "granted_by": granted_by},
                    )
        except Exception as exc:  # noqa: BLE001 — показываем админу причину по строке
            errors.append(f"Строка {line}: {exc}")
            continue
        created += is_new
        updated += not is_new
    return created, updated, errors


@admin.register(TradePoint)
class TradePointAdmin(admin.ModelAdmin):
    form = TradePointForm
    list_display = ("code", "name", "block", "place_number", "is_active", "has_qr", "created_at")
    list_filter = ("is_active", "block")
    search_fields = ("code", "name", "place_number", "address", "external_id_1c")
    ordering = ("block", "place_number", "code")
    inlines = [MeterInline, AccessInline]
    readonly_fields = ("created_at", "updated_at")
    save_on_top = True
    actions = ["deactivate"]
    change_list_template = "admin/trade_points/tradepoint/change_list.html"
    fieldsets = (
        (_("Основное"), {"fields": ("name", "block", "place_number", "code")}),
        (
            _("Дополнительно"),
            {
                "classes": ("collapse",),
                "fields": (
                    "address",
                    "area_m2",
                    "is_active",
                    "qr_payload",
                    "external_id_1c",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
    )

    @admin.display(boolean=True, description=_("QR"))
    def has_qr(self, obj: TradePoint) -> bool:
        return bool(obj.qr_data)

    @admin.action(description=_("Деактивировать выбранные точки"))
    def deactivate(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"Деактивировано точек: {updated}")

    def get_changeform_initial_data(self, request):
        # «Сохранить и добавить другую» — корпус переносится в новую форму.
        initial = super().get_changeform_initial_data(request)
        if "block" not in initial and (last := request.session.get("tradepoint_last_block")):
            initial["block"] = last
        return initial

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        request.session["tradepoint_last_block"] = obj.block

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for instance in instances:
            if isinstance(instance, TradePointAccess) and instance.granted_by_id is None:
                instance.granted_by = request.user
            instance.save()
        for obj in formset.deleted_objects:
            obj.delete()
        formset.save_m2m()

    def get_urls(self):
        custom = [
            path(
                "import/",
                self.admin_site.admin_view(self.import_view),
                name="trade_points_tradepoint_import",
            )
        ]
        return custom + super().get_urls()

    def import_view(self, request):
        if not self.has_add_permission(request):
            return redirect("admin:trade_points_tradepoint_changelist")
        form = ImportForm(request.POST or None, request.FILES or None)
        if request.method == "POST" and form.is_valid():
            raw = form.cleaned_data["file"].read()
            for encoding in ("utf-8-sig", "cp1251"):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                messages.error(request, _("Не удалось прочитать файл: сохраните его как CSV UTF-8."))
                return redirect(request.path)
            created, updated, errors = import_trade_points(text, request.user)
            messages.success(request, f"Создано точек: {created}, обновлено: {updated}.")
            for error in errors[:30]:
                messages.warning(request, error)
            if len(errors) > 30:
                messages.warning(request, f"…и ещё ошибок: {len(errors) - 30}")
            return redirect("admin:trade_points_tradepoint_changelist")
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": _("Импорт торговых точек из CSV"),
            "form": form,
            "columns": IMPORT_COLUMNS,
            "changelist_url": reverse("admin:trade_points_tradepoint_changelist"),
        }
        return render(request, "admin/trade_points/tradepoint/import.html", context)


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
    autocomplete_fields = ("trade_point",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(TradePointAccess)
class TradePointAccessAdmin(admin.ModelAdmin):
    list_display = ("user", "trade_point", "access_role", "is_active", "granted_by", "created_at")
    list_filter = ("access_role", "is_active")
    search_fields = ("user__inn", "user__phone", "user__username", "trade_point__code", "trade_point__name")
    autocomplete_fields = ("user", "trade_point")
    readonly_fields = ("created_at", "updated_at")
