from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect
from django.template.defaultfilters import timesince
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from apps.users.models import UserRole

from .models import AppRelease, AppVersionCheckIn, Platform, ReleaseStatus
from .services import (
    ACTION_LABELS,
    ROLLOUT_PRESETS,
    RolloutError,
    adoption_stats,
    available_actions,
    release_stats,
    transition,
)

STATUS_ICONS = {
    ReleaseStatus.DRAFT: "fas fa-pen",
    ReleaseStatus.ROLLING_OUT: "fas fa-rocket",
    ReleaseStatus.PAUSED: "fas fa-pause",
    ReleaseStatus.COMPLETED: "fas fa-check",
    ReleaseStatus.ROLLED_BACK: "fas fa-undo-alt",
}
PLATFORM_ICONS = {Platform.ANDROID: "fab fa-android", Platform.IOS: "fab fa-apple"}

# Подписи, иконки и тексты подтверждения кнопок быстрых действий.
ACTION_UI = {
    "start": {
        "label": _("Начать раскатку"),
        "icon": "fas fa-rocket",
        "tone": "primary",
        "confirm": _("Обновление начнут получать пользователи в пределах выбранного процента."),
    },
    "pause": {
        "label": _("Приостановить"),
        "icon": "fas fa-pause",
        "tone": "warning",
        "confirm": _("Новые пользователи перестанут получать предложение обновиться. Процент сохранится."),
    },
    "resume": {
        "label": _("Возобновить"),
        "icon": "fas fa-play",
        "tone": "primary",
        "confirm": _("Раскатка продолжится с текущим процентом."),
    },
    "complete": {
        "label": _("Выпустить для всех"),
        "icon": "fas fa-check-double",
        "tone": "success",
        "confirm": _("Процент станет 100% — обновление получат все пользователи платформы."),
    },
    "roll_back": {
        "label": _("Отозвать"),
        "icon": "fas fa-undo-alt",
        "tone": "danger",
        "confirm": _(
            "Релиз перестанет предлагаться. Те, кто уже обновился, останутся на этой сборке — "
            "для их исправления выпустите новую сборку."
        ),
    },
}


def status_badge_html(obj: AppRelease, size: str = "") -> str:
    return format_html(
        '<span class="rr-status rr-status--{} {}"><i class="{}" aria-hidden="true"></i>{}</span>',
        obj.status,
        f"rr-status--{size}" if size else "",
        STATUS_ICONS.get(obj.status, "fas fa-circle"),
        obj.get_status_display(),
    )


class AppReleaseForm(forms.ModelForm):
    store_url = forms.URLField(
        label=_("Ссылка на магазин"),
        max_length=500,
        assume_scheme="https",
        widget=forms.URLInput(attrs={"class": "vURLField", "style": "width:100%"}),
        help_text=_("Google Play или App Store"),
    )
    target_roles = forms.MultipleChoiceField(
        label=_("Только для ролей"),
        choices=UserRole.choices,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Не отмечено — для всех. Например, сначала выпустить только контролёрам."),
    )

    class Meta:
        model = AppRelease
        fields = "__all__"
        widgets = {
            "release_notes": forms.Textarea(attrs={"rows": 5, "placeholder": "Что нового:\n• …"}),
            "release_notes_ky": forms.Textarea(attrs={"rows": 4}),
            "allowlisted_device_ids": forms.Textarea(attrs={"rows": 3, "placeholder": "device-id-1\ndevice-id-2"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk and "build_number" in self.fields:
            platform = self.initial.get("platform") or Platform.ANDROID
            latest = (
                AppRelease.objects.filter(platform=platform)
                .order_by("-build_number")
                .values_list("build_number", flat=True)
                .first()
            )
            if latest is not None:
                self.fields["build_number"].help_text = format_html(
                    "{} <b>{}</b>.", _("Должен быть больше последней сборки платформы:"), latest
                )

    def clean_target_roles(self):
        return list(self.cleaned_data.get("target_roles") or [])


@admin.register(AppRelease)
class AppReleaseAdmin(admin.ModelAdmin):
    form = AppReleaseForm
    change_form_template = "admin/app_releases/apprelease/change_form.html"
    change_list_template = "admin/app_releases/apprelease/change_list.html"
    list_display = (
        "release_title",
        "status_badge",
        "rollout_bar",
        "mandatory_badge",
        "min_supported_build",
        "started_display",
    )
    list_display_links = ("release_title",)
    list_filter = ("platform", "status", "is_mandatory")
    search_fields = ("version", "build_number", "release_notes")
    autocomplete_fields = ("allowlisted_users",)
    readonly_fields = ("created_by", "started_at", "completed_at", "rolled_back_at", "created_at", "updated_at")
    actions = ["pause_selected"]
    save_on_top = False
    fieldsets = (
        (_("Сборка"), {"fields": ("platform", "version", "build_number", "store_url")}),
        (_("Что нового"), {"fields": ("release_notes", "release_notes_ky")}),
        (
            _("Раскатка"),
            {"fields": ("status", "rollout_percentage", "is_mandatory", "min_supported_build")},
        ),
        (
            _("Тестировщики и роли"),
            {"fields": ("allowlisted_users", "allowlisted_device_ids", "target_roles")},
        ),
        (
            _("Служебное"),
            {"fields": ("created_by", "started_at", "completed_at", "rolled_back_at", "created_at", "updated_at")},
        ),
    )

    class Media:
        css = {"all": ("app_releases/rollout.css",)}

    # --- Форма -----------------------------------------------------------
    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        # Статус меняется только кнопками раскатки (с журналом и проверками).
        fields.append("status")
        if obj is not None:
            fields += ["platform", "build_number"]
            if obj.status != ReleaseStatus.DRAFT:
                fields.append("rollout_percentage")
        return fields

    def save_model(self, request, obj, form, change):
        if not change and obj.created_by_id is None:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    # --- Колонки списка --------------------------------------------------
    @admin.display(description=_("Релиз"), ordering="build_number")
    def release_title(self, obj: AppRelease):
        return format_html(
            '<span class="rr-release"><i class="{} rr-release__icon rr-release__icon--{}" aria-hidden="true"></i>'
            '<span><b>{}</b><small>{} · сборка {}</small></span></span>',
            PLATFORM_ICONS.get(obj.platform, "fas fa-mobile-alt"),
            obj.platform,
            obj.version,
            obj.get_platform_display(),
            obj.build_number,
        )

    @admin.display(description=_("Статус"), ordering="status")
    def status_badge(self, obj: AppRelease):
        return status_badge_html(obj)

    @admin.display(description=_("Раскатка"), ordering="rollout_percentage")
    def rollout_bar(self, obj: AppRelease):
        pct = obj.effective_percentage
        return format_html(
            '<div class="rr-progress rr-progress--{}" role="progressbar" aria-valuenow="{}" aria-valuemin="0" '
            'aria-valuemax="100" aria-label="Процент раскатки"><div class="rr-progress__track">'
            '<div class="rr-progress__fill" style="width:{}%"></div></div><span class="rr-progress__value">{}%</span></div>',
            obj.status,
            pct,
            pct,
            pct,
        )

    @admin.display(description=_("Обязательное"), ordering="is_mandatory")
    def mandatory_badge(self, obj: AppRelease):
        if not obj.is_mandatory:
            return format_html('<span class="rr-muted">{}</span>', "—")
        return format_html(
            '<span class="rr-tag rr-tag--danger"><i class="fas fa-exclamation-triangle" aria-hidden="true"></i>{}</span>',
            _("Обязательное"),
        )

    @admin.display(description=_("Начата"), ordering="started_at")
    def started_display(self, obj: AppRelease):
        if not obj.started_at:
            return format_html('<span class="rr-muted">{}</span>', _("не начата"))
        return format_html(
            '<span title="{}">{} назад</span>',
            timezone.localtime(obj.started_at).strftime("%d.%m.%Y %H:%M"),
            timesince(obj.started_at).split(",")[0],
        )

    @admin.action(description=_("Приостановить выбранные раскатки"), permissions=["change"])
    def pause_selected(self, request, queryset):
        done = 0
        for release in queryset.filter(status=ReleaseStatus.ROLLING_OUT):
            transition(release, "pause", user=request.user, request=request)
            done += 1
        self.message_user(request, f"Приостановлено раскаток: {done}", messages.SUCCESS if done else messages.INFO)

    # --- Свои URL --------------------------------------------------------
    def get_urls(self):
        custom = [
            path(
                "<path:object_id>/rollout/",
                self.admin_site.admin_view(self.rollout_view),
                name="app_releases_apprelease_rollout",
            )
        ]
        return custom + super().get_urls()

    def rollout_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        release = get_object_or_404(AppRelease, pk=object_id)
        if not self.has_change_permission(request, release):
            raise PermissionDenied
        action = request.POST.get("action", "")
        percentage = request.POST.get("percentage")
        try:
            if action == "set_percentage":
                release = transition(release, action, user=request.user, request=request, percentage=percentage)
            else:
                release = transition(
                    release,
                    action,
                    user=request.user,
                    request=request,
                    percentage=percentage if action == "start" and percentage not in (None, "") else None,
                )
        except (RolloutError, ValidationError) as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            label = ACTION_LABELS.get(action, action)
            messages.success(request, f"{label}: {release} — {release.effective_percentage}%.")
        return redirect(reverse("admin:app_releases_apprelease_change", args=[release.pk]))

    # --- Контекст панели -------------------------------------------------
    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        release = self.get_object(request, object_id)
        if release is not None:
            extra_context["rollout"] = self._panel_context(request, release)
        return super().change_view(request, object_id, form_url, extra_context)

    def _panel_context(self, request, release: AppRelease) -> dict:
        stats = release_stats(release)
        can_change = self.has_change_permission(request, release)
        locked_min = release.rollout_percentage if release.status != ReleaseStatus.DRAFT else 0
        slider_enabled = can_change and release.status in (
            ReleaseStatus.DRAFT,
            ReleaseStatus.ROLLING_OUT,
            ReleaseStatus.PAUSED,
        )
        actions = [
            {"code": code, **ACTION_UI[code]} for code in available_actions(release) if can_change
        ]
        chart = [
            {
                "build": row["build_number"],
                "version": row["version"] or "",
                "devices": row["devices"],
                "share": row["share"],
                "current": row["build_number"] == release.build_number,
                "newer": row["build_number"] > release.build_number,
            }
            for row in stats["adoption"]["builds"][:12]
        ]
        events = list(release.events.select_related("user")[:15])
        for event in events:
            event.label = ACTION_LABELS.get(event.action, event.action)
            event.to_status_label = ReleaseStatus(event.to_status).label if event.to_status else ""
        return {
            "release": release,
            "status_badge": status_badge_html(release, "lg"),
            "platform_icon": PLATFORM_ICONS.get(release.platform),
            "pct": release.effective_percentage,
            "stats": stats,
            "chart": chart,
            "presets": [{"value": p, "locked": p < locked_min} for p in ROLLOUT_PRESETS],
            "locked_min": locked_min,
            "slider_enabled": slider_enabled,
            "actions": actions,
            "events": events,
            "action_url": reverse("admin:app_releases_apprelease_rollout", args=[release.pk]),
            "running_for": timesince(release.started_at) if release.started_at else None,
            "target_roles": [UserRole(r).label for r in release.target_roles or [] if r in UserRole.values],
        }

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        overview = []
        for value, label in Platform.choices:
            live = (
                AppRelease.objects.filter(platform=value)
                .exclude(status__in=[ReleaseStatus.DRAFT, ReleaseStatus.ROLLED_BACK])
                .order_by("-build_number")
                .first()
            )
            adoption = adoption_stats(value)
            overview.append(
                {
                    "platform": value,
                    "label": label,
                    "icon": PLATFORM_ICONS[value],
                    "live": live,
                    "live_badge": status_badge_html(live) if live else "",
                    "devices": adoption["total"],
                    "top_builds": adoption["builds"][:4],
                }
            )
        extra_context["rollout_overview"] = overview
        return super().changelist_view(request, extra_context)


@admin.register(AppVersionCheckIn)
class AppVersionCheckInAdmin(admin.ModelAdmin):
    list_display = ("platform", "version", "build_number", "offered_build", "user", "check_count", "last_seen_at")
    list_filter = ("platform", "build_number")
    search_fields = ("version", "user__phone", "user__inn")
    date_hierarchy = "last_seen_at"
    list_select_related = ("user",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ===========================================================================
# Фича-флаги
# ===========================================================================
import json  # noqa: E402
import re  # noqa: E402

from django.db.models import Count, Q  # noqa: E402
from django.template.response import TemplateResponse  # noqa: E402

from apps.users.models import User  # noqa: E402

from .flags import (  # noqa: E402
    ACTION_LABELS as FLAG_ACTION_LABELS,
    FLAG_PRESETS,
    REASONS,
    FlagError,
    apply_flag_action,
    evaluate_all,
    flag_reach,
    log_flag_event,
    reach_index,
)
from .models import FeatureFlag, FlagPlatform  # noqa: E402
from .services import RolloutSubject, hash_key, resolve_update  # noqa: E402

FLAG_STATE_ICONS = {
    "off": "fas fa-power-off",
    "partial": "fas fa-adjust",
    "full": "fas fa-check",
    "archived": "fas fa-archive",
}


def flag_state_badge(flag: FeatureFlag, size: str = "") -> str:
    return format_html(
        '<span class="rr-status rr-status--flag-{} {}"><i class="{}" aria-hidden="true"></i>{}</span>',
        flag.state,
        f"rr-status--{size}" if size else "",
        FLAG_STATE_ICONS[flag.state],
        flag.get_state_display(),
    )


class PrettyJSONWidget(forms.Textarea):
    def format_value(self, value):
        if value in (None, ""):
            return ""
        try:
            return json.dumps(json.loads(value) if isinstance(value, str) else value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return value


class FeatureFlagForm(forms.ModelForm):
    target_roles = forms.MultipleChoiceField(
        label=_("Только для ролей"),
        choices=UserRole.choices,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Не отмечено — для всех ролей и анонимных устройств."),
    )

    class Meta:
        model = FeatureFlag
        fields = "__all__"
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "allowlisted_device_ids": forms.Textarea(attrs={"rows": 3, "placeholder": "device-id-1\ndevice-id-2"}),
            "payload": PrettyJSONWidget(attrs={"rows": 5, "class": "rr-code", "placeholder": '{"variant": "b"}'}),
        }

    def clean_target_roles(self):
        return list(self.cleaned_data.get("target_roles") or [])


@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    form = FeatureFlagForm
    change_list_template = "admin/app_releases/featureflag/change_list.html"
    change_form_template = "admin/app_releases/featureflag/change_form.html"
    list_display = ("flag_title", "targeting_cell", "updated_at")  # полный набор — в get_list_display
    list_display_links = ("flag_title",)
    list_filter = ("is_enabled", "platform", "is_archived")
    search_fields = ("key", "name", "description")
    autocomplete_fields = ("allowlisted_users",)
    readonly_fields = ("created_by", "enabled_at", "disabled_at", "created_at", "updated_at")
    fieldsets = (
        (_("Флаг"), {"fields": ("name", "key", "description", "is_enabled", "rollout_percentage", "payload")}),
        (_("Кому"), {"fields": ("platform", "min_build", "max_build", "target_roles")}),
        (_("Тестировщики"), {"fields": ("allowlisted_users", "allowlisted_device_ids")}),
        (_("Служебное"), {"fields": ("is_archived", "created_by", "enabled_at", "disabled_at", "created_at", "updated_at")}),
    )

    class Media:
        css = {"all": ("app_releases/rollout.css",)}
        js = ("app_releases/flags.js",)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            # Ключ зашит в коде приложения. Включение и процент меняются только кнопками
            # (apply_flag_action): устаревшая форма не должна отменить аварийное выключение.
            fields += ["key", "is_enabled", "rollout_percentage"]
        return fields

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(allowlist_count=Count("allowlisted_users", distinct=True))

    # --- Аудит сохранений из формы --------------------------------------
    def save_model(self, request, obj, form, change):
        now = timezone.now()
        if not change:
            obj.created_by = request.user
            if obj.is_enabled:
                obj.enabled_at = now
        super().save_model(request, obj, form, change)
        if not change:
            log_flag_event(obj, "create", f"{'вкл' if obj.is_enabled else 'выкл'}, {obj.rollout_percentage}%",
                           user=request.user, request=request)
        elif form.changed_data:
            labels = [str(form.fields[name].label) for name in form.changed_data if name in form.fields]
            log_flag_event(obj, "update", "поля: " + ", ".join(labels), user=request.user, request=request)

    # --- Колонки ---------------------------------------------------------
    @admin.display(description=_("Флаг"), ordering="key")
    def flag_title(self, obj: FeatureFlag):
        return format_html(
            '<span class="ff-title"><b>{}</b><code>{}</code></span>', obj.name, obj.key
        )

    def _toggle_html(self, request, obj: FeatureFlag, large=False):
        url = reverse("admin:app_releases_featureflag_quick", args=[obj.pk])
        on = obj.is_enabled
        disabled = obj.is_archived or not self.has_change_permission(request, obj)
        if on:
            attrs = {
                "action": "disable", "tone": "danger", "icon": "fa-power-off",
                "title": f"Выключить «{obj.name}»?",
                "text": "Флаг станет false у всех пользователей, включая тестировщиков, при следующем запросе приложения.",
                "confirm": "Выключить",
            }
        else:
            attrs = {
                "action": "enable", "tone": "success", "icon": "fa-toggle-on",
                "title": f"Включить «{obj.name}»?",
                "text": f"Флаг получат {obj.rollout_percentage}% пользователей и тестировщики.",
                "confirm": "Включить",
            }
        return format_html(
            '<button type="button" class="ff-switch{}{}" role="switch" aria-checked="{}" aria-label="{}" {} '
            'data-ff-submit data-ff-url="{}" data-ff-action="{}" data-ff-tone="{}" data-ff-icon="{}" '
            'data-ff-title="{}" data-ff-text="{}" data-ff-confirm="{}">'
            '<span class="ff-switch__track"><span class="ff-switch__thumb"></span></span>'
            '<span class="ff-switch__label">{}</span></button>',
            " is-on" if on else "",
            " ff-switch--lg" if large else "",
            "true" if on else "false",
            f"Флаг {obj.key}",
            "disabled" if disabled else "",
            url, attrs["action"], attrs["tone"], attrs["icon"], attrs["title"], attrs["text"], attrs["confirm"],
            "Вкл" if on else "Выкл",
        )

    def _chips_html(self, request, obj: FeatureFlag, compact=True):
        if obj.is_archived or not self.has_change_permission(request, obj):
            return ""
        url = reverse("admin:app_releases_featureflag_quick", args=[obj.pk])
        chips = []
        for value in FLAG_PRESETS:
            active = value == obj.rollout_percentage
            direction = "Увеличить" if value > obj.rollout_percentage else "Уменьшить"
            if value < obj.rollout_percentage:
                text = (f"Флаг пропадёт у части пользователей: останутся только попавшие в {value}%. "
                        "Пользователи, оставшиеся в выборке, не изменятся.")
            else:
                text = (f"Флаг получат {value}% пользователей. Все, кто уже его получил, его сохранят."
                        + ("" if obj.is_enabled else " Флаг сейчас выключен — процент вступит в силу после включения."))
            chips.append(
                format_html(
                    '<button type="button" class="rr-chip{}{}" {} data-ff-submit data-ff-url="{}" '
                    'data-ff-action="set_percentage" data-ff-percentage="{}" data-ff-tone="{}" data-ff-icon="{}" '
                    'data-ff-title="{} до {}%?" data-ff-text="{}" data-ff-confirm="{}" aria-pressed="{}">{}%</button>',
                    " rr-chip--sm" if compact else "",
                    " is-active" if active else "",
                    "disabled" if active else "",
                    url, value,
                    "warning" if value < obj.rollout_percentage else "primary",
                    "fa-arrow-down" if value < obj.rollout_percentage else "fa-arrow-up",
                    direction, value, text, direction,
                    "true" if active else "false",
                    value,
                )
            )
        return format_html('<div class="rr-chips rr-chips--inline" role="group" aria-label="Процент">{}</div>',
                           mark_safe("".join(chips)))  # noqa: S308 — элементы уже экранированы

    def get_list_display(self, request):
        # Колонки с кнопками зависят от прав текущего пользователя: привязываем request
        # замыканием, а не атрибутом ModelAdmin (он общий для всех потоков).
        @admin.display(description=_("Состояние"), ordering="is_enabled")
        def toggle_cell(obj):
            return self._toggle_html(request, obj)

        @admin.display(description=_("Процент пользователей"), ordering="rollout_percentage")
        def rollout_cell(obj):
            return self._rollout_html(request, obj)

        index = []  # агрегат проверок — один запрос на страницу, не на строку

        @admin.display(description=_("Охват"))
        def reach_cell(obj):
            if not index:
                index.append(reach_index())
            return self._reach_html(obj, index[0])

        return ("flag_title", toggle_cell, rollout_cell, reach_cell, "targeting_cell", "updated_at")

    def _rollout_html(self, request, obj: FeatureFlag):
        tone = "completed" if obj.rollout_percentage == 100 else "rolling_out"
        if not obj.is_enabled or obj.is_archived:
            tone = "off"
        bar = format_html(
            '<div class="rr-progress rr-progress--{}" role="progressbar" aria-valuenow="{}" aria-valuemin="0" '
            'aria-valuemax="100" aria-label="Процент пользователей"><div class="rr-progress__track">'
            '<div class="rr-progress__fill" style="width:{}%"></div></div><span class="rr-progress__value">{}%</span></div>',
            tone, obj.rollout_percentage, obj.rollout_percentage, obj.rollout_percentage,
        )
        return format_html('<div class="ff-rollout">{}{}</div>', bar, self._chips_html(request, obj))

    def _reach_html(self, obj: FeatureFlag, index):
        reach = flag_reach(obj, index)
        return format_html(
            '<span class="ff-reach" title="Оценка по устройствам, проверявшим обновления за 30 дней">'
            '<b>≈ {}</b><small>из {} подходящих</small></span>',
            reach["estimated"], reach["eligible"],
        )

    @admin.display(description=_("Таргетинг"))
    def targeting_cell(self, obj: FeatureFlag):
        tags = []
        if obj.platform != FlagPlatform.ALL:
            tags.append(format_html('<span class="rr-tag"><i class="{}" aria-hidden="true"></i>{}</span>',
                                    PLATFORM_ICONS.get(obj.platform, ""), obj.get_platform_display()))
        if obj.min_build is not None or obj.max_build is not None:
            span = f"{obj.min_build or '…'}–{obj.max_build or '…'}"
            tags.append(format_html('<span class="rr-tag"><i class="fas fa-code-branch" aria-hidden="true"></i>{}</span>', span))
        for role in obj.target_roles or []:
            if role in UserRole.values:
                tags.append(format_html('<span class="rr-tag rr-tag--info">{}</span>', UserRole(role).label))
        if obj.payload is not None:
            tags.append(format_html('<span class="rr-tag"><i class="fas fa-code" aria-hidden="true"></i>{}</span>', "payload"))
        if not tags:
            return format_html('<span class="rr-muted">{}</span>', "все")
        return format_html('<div class="ff-tags">{}</div>', mark_safe("".join(tags)))  # noqa: S308

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["check_user_url"] = self._check_user_url(request)
        return super().changelist_view(request, extra_context)

    def _check_user_url(self, request):
        if request.user.has_perm("users.view_user"):
            return reverse("admin:app_releases_featureflag_check_user")
        return ""

    # --- Свои URL --------------------------------------------------------
    def get_urls(self):
        custom = [
            path("check-user/", self.admin_site.admin_view(self.check_user_view),
                 name="app_releases_featureflag_check_user"),
            path("<path:object_id>/quick/", self.admin_site.admin_view(self.quick_view),
                 name="app_releases_featureflag_quick"),
        ]
        return custom + super().get_urls()

    def quick_view(self, request, object_id):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        flag = get_object_or_404(FeatureFlag, pk=object_id)
        if not self.has_change_permission(request, flag):
            raise PermissionDenied
        action = request.POST.get("action", "")
        try:
            flag = apply_flag_action(flag, action, percentage=request.POST.get("percentage"),
                                     user=request.user, request=request)
        except (FlagError, ValidationError) as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            label = FLAG_ACTION_LABELS.get(action, action)
            messages.success(request, f"{label}: «{flag.name}» — {flag.get_state_display().lower()}.")
        next_url = request.POST.get("next") or ""
        if not next_url.startswith("/admin/"):
            next_url = reverse("admin:app_releases_featureflag_changelist")
        return redirect(next_url)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        flag = self.get_object(request, object_id)
        if flag is not None:
            events = list(flag.events.select_related("user")[:15])
            for event in events:
                event.label = FLAG_ACTION_LABELS.get(event.action, event.action)
            extra_context["ff"] = {
                "flag": flag,
                "badge": flag_state_badge(flag, "lg"),
                "toggle": self._toggle_html(request, flag, large=True),
                "chips": self._chips_html(request, flag, compact=False),
                "reach": flag_reach(flag),
                "events": events,
                "quick_url": reverse("admin:app_releases_featureflag_quick", args=[flag.pk]),
                "can_change": self.has_change_permission(request, flag) and not flag.is_archived,
                "payload_json": json.dumps(flag.payload, ensure_ascii=False, indent=2) if flag.payload is not None else "",
                "check_user_url": self._check_user_url(request),
                "target_roles": [UserRole(r).label for r in flag.target_roles or [] if r in UserRole.values],
            }
        return super().change_view(request, object_id, form_url, extra_context)

    # --- Проверка пользователя -------------------------------------------
    def check_user_view(self, request):
        # Инструмент раскрывает данные пользователей — нужен ещё и просмотр пользователей.
        if not (self.has_view_permission(request) and request.user.has_perm("users.view_user")):
            raise PermissionDenied
        query = (request.GET.get("q") or "").strip()
        platform = request.GET.get("platform") or ""
        build_raw = (request.GET.get("build") or "").strip()
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": _("Проверка пользователя"),
            "q": query,
            "platform": platform,
            "build": build_raw,
            "platforms": [("", "Как в последнем визите")] + list(Platform.choices),
            "changelist_url": reverse("admin:app_releases_featureflag_changelist"),
        }
        if query:
            context.update(self._check_user(query, platform, build_raw))
        return TemplateResponse(request, "admin/app_releases/featureflag/check_user.html", context)

    def _check_user(self, query: str, platform: str, build_raw: str) -> dict:
        digits = re.sub(r"[^\d+]", "", query)
        lookup = Q(inn=query) | Q(username=query)
        if digits:
            lookup |= Q(phone=digits) | Q(phone="+" + digits.lstrip("+"))
        user = User.objects.filter(lookup).first()
        if user is not None:
            subject = RolloutSubject(user=user)
            check_in = user.app_check_ins.order_by("-last_seen_at").first()
        else:
            subject = RolloutSubject(device_id=query)
            check_in = AppVersionCheckIn.objects.filter(rollout_key_hash=hash_key(f"device:{query}")).order_by(
                "-last_seen_at").first()
        build = int(build_raw) if build_raw.isdigit() else (check_in.build_number if check_in else None)
        platform = platform or (check_in.platform if check_in else Platform.ANDROID)
        rows = []
        for flag, result in evaluate_all(subject, platform=platform, build=build):
            rows.append({
                "flag": flag,
                "result": result,
                "reason": REASONS.get(result.reason, result.reason),
                "payload": json.dumps(flag.payload, ensure_ascii=False) if result.value and flag.payload is not None else "",
                "url": reverse("admin:app_releases_featureflag_change", args=[flag.pk]),
            })
        decision = resolve_update(platform, build, subject) if build is not None else None
        return {
            "subject_user": user,
            "rollout_key": subject.rollout_key,
            "check_in": check_in,
            "eff_platform": platform,
            "eff_platform_label": Platform(platform).label if platform in Platform.values else platform,
            "eff_build": build,
            "rows": rows,
            "on_count": sum(1 for r in rows if r["result"].value),
            "decision": decision,
        }
