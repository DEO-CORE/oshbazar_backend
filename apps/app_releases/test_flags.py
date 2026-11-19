import hashlib
import uuid

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.core.testing import make_admin, make_controller, make_superuser, make_tenant

from .flags import apply_flag_action, evaluate_flag, flag_bucket, flag_reach
from .models import AppVersionCheckIn, FeatureFlag, FeatureFlagEvent
from .services import RolloutSubject, hash_key

URL = "/api/v1/app/features/"


def make_flag(key="new_payment_screen", pct=100, enabled=True, **extra):
    extra.setdefault("name", key.replace("_", " ").capitalize())
    return FeatureFlag.objects.create(key=key, rollout_percentage=pct, is_enabled=enabled, **extra)


def U(name) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"device:{name}"))


def ev(flag, subject=None, platform="android", build=20):
    return evaluate_flag(flag, subject or RolloutSubject(device_id="dev"), platform=platform, build=build).value


def device_for(flag, inside=True):
    for i in range(5000):
        key = U(f"probe-{i}")
        if (flag_bucket(flag.key, key) < flag.rollout_percentage) == inside:
            return key
    raise AssertionError("no key found")


class FlagBucketingTests(TestCase):
    def test_formula_and_determinism(self):
        expected = int(hashlib.sha256(b"dark_map:user-1").hexdigest(), 16) % 100
        self.assertEqual({flag_bucket("dark_map", "user-1") for _ in range(5)}, {expected})

    def test_raising_percentage_only_adds_users(self):
        flag = make_flag(pct=1)
        subjects = [RolloutSubject(device_id=f"d{i}") for i in range(500)]
        previous = {s.device_id for s in subjects if ev(flag, s)}
        for pct in (5, 10, 25, 50, 100):
            flag = apply_flag_action(flag, "set_percentage", percentage=pct)
            current = {s.device_id for s in subjects if ev(flag, s)}
            self.assertTrue(previous <= current, f"users dropped at {pct}%")
            previous = current
        self.assertEqual(len(previous), 500)

    def test_share_is_close_to_percentage(self):
        flag = make_flag(pct=10)
        share = sum(ev(flag, RolloutSubject(device_id=f"x{i}")) for i in range(4000)) / 4000
        self.assertAlmostEqual(share, 0.10, delta=0.02)

    def test_different_flags_bucket_independently(self):
        same = sum(flag_bucket("a_flag", f"k{i}") == flag_bucket("b_flag", f"k{i}") for i in range(200))
        self.assertLess(same, 20)


class FlagFilterTests(TestCase):
    def test_kill_switch_beats_everything(self):
        tester = make_tenant()
        flag = make_flag(pct=100, allowlisted_device_ids="dev")
        flag.allowlisted_users.add(tester)
        flag = apply_flag_action(flag, "disable")
        self.assertFalse(ev(flag))
        self.assertFalse(ev(flag, RolloutSubject(user=tester)))

    def test_platform_filter(self):
        flag = make_flag(platform="ios")
        self.assertFalse(ev(flag, platform="android"))
        self.assertTrue(ev(flag, platform="ios"))

    def test_build_range(self):
        flag = make_flag(min_build=15, max_build=20)
        self.assertFalse(ev(flag, build=14))
        self.assertTrue(ev(flag, build=15))
        self.assertTrue(ev(flag, build=20))
        self.assertFalse(ev(flag, build=21))

    def test_role_targeting(self):
        flag = make_flag(target_roles=["controller", "admin"])
        self.assertTrue(ev(flag, RolloutSubject(user=make_controller())))
        self.assertTrue(ev(flag, RolloutSubject(user=make_admin())))
        self.assertFalse(ev(flag, RolloutSubject(user=make_tenant())))
        self.assertFalse(ev(flag, RolloutSubject(device_id="anon")))

    def test_allowlist_overrides_percentage_and_roles(self):
        tester = make_tenant()
        flag = make_flag(pct=0, target_roles=["controller"], allowlisted_device_ids="qa-phone")
        flag.allowlisted_users.add(tester)
        self.assertTrue(ev(flag, RolloutSubject(user=tester)))
        self.assertTrue(ev(flag, RolloutSubject(device_id="qa-phone")))
        self.assertFalse(ev(flag, RolloutSubject(device_id="other")))

    def test_allowlist_still_respects_platform_and_build(self):
        flag = make_flag(pct=0, platform="ios", allowlisted_device_ids="qa")
        self.assertFalse(ev(flag, RolloutSubject(device_id="qa"), platform="android"))

    def test_no_key_only_full_rollout(self):
        self.assertFalse(ev(make_flag(key="half", pct=99), RolloutSubject()))
        self.assertTrue(ev(make_flag(key="full", pct=100), RolloutSubject()))


@override_settings(APP_VERSION_CHECK_THROTTLE_RATE="1000/min")
class FeaturesEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def get(self, **params):
        query = {"platform": "android", "build": 20, "version": "1.2.0", "device_id": U("dev-1"), **params}
        response = self.client.get(URL, query)
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def test_exact_response_shape(self):
        make_flag("new_payment_screen", pct=100, payload={"variant": "b"})
        make_flag("dark_map", enabled=False, payload={"style": "night"})
        make_flag("old_flag", is_archived=True)
        response = self.get()
        self.assertEqual(
            response.json(),
            {
                "flags": {"dark_map": False, "new_payment_screen": True},
                "payloads": {"new_payment_screen": {"variant": "b"}},
                "ttl_seconds": 300,
            },
        )
        self.assertIn("private", response["Cache-Control"])

    def test_empty(self):
        self.assertEqual(self.get().json(), {"flags": {}, "payloads": {}, "ttl_seconds": 300})

    @override_settings(FEATURE_FLAGS_TTL_SECONDS=60)
    def test_ttl_configurable(self):
        self.assertEqual(self.get().json()["ttl_seconds"], 60)

    def test_payload_only_for_true(self):
        flag = make_flag(pct=50, payload={"v": 1})
        inside, outside = device_for(flag, True), device_for(flag, False)
        self.assertEqual(self.get(device_id=inside).json()["payloads"], {flag.key: {"v": 1}})
        data = self.get(device_id=outside).json()
        self.assertEqual(data["flags"], {flag.key: False})
        self.assertEqual(data["payloads"], {})

    def test_kill_switch_is_instant(self):
        flag = make_flag(pct=100)
        self.assertTrue(self.get().json()["flags"][flag.key])
        apply_flag_action(flag, "disable")
        self.assertFalse(self.get().json()["flags"][flag.key])

    def test_authenticated_key_is_user_id(self):
        user = make_tenant()
        flag = make_flag(pct=50)
        expected = flag_bucket(flag.key, str(user.pk)) < 50
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
        values = {self.get(device_id=U(f"dev-{i}")).json()["flags"][flag.key] for i in range(8)}
        self.assertEqual(values, {expected})
        self.assertEqual(AppVersionCheckIn.objects.get().user, user)

    def test_anonymous_key_is_device_id(self):
        flag = make_flag(pct=50)
        for device in (device_for(flag, True), device_for(flag, False)):
            expected = flag_bucket(flag.key, device) < 50
            self.assertEqual(self.get(device_id=device).json()["flags"][flag.key], expected)
        self.assertEqual(AppVersionCheckIn.objects.filter(user__isnull=True).count(), 2)

    def test_role_filter_via_endpoint(self):
        make_flag("controller_tools", target_roles=["controller"])
        self.assertFalse(self.get().json()["flags"]["controller_tools"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(make_controller())}")
        self.assertTrue(self.get().json()["flags"]["controller_tools"])

    def test_platform_and_build_via_endpoint(self):
        make_flag("ios_only", platform="ios")
        make_flag("new_builds", min_build=25)
        flags = self.get(platform="android", build=20).json()["flags"]
        self.assertEqual(flags, {"ios_only": False, "new_builds": False})
        flags = self.get(platform="ios", build=25).json()["flags"]
        self.assertEqual(flags, {"ios_only": True, "new_builds": True})

    def test_features_check_in_does_not_clear_offered_build(self):
        AppVersionCheckIn.objects.create(platform="android", rollout_key_hash=hash_key(f"device:{U('dev-1')}"),
                                         build_number=20, offered_build=21, last_seen_at=timezone.now())
        self.get()
        row = AppVersionCheckIn.objects.get()
        self.assertEqual((row.offered_build, row.check_count), (21, 2))

    def test_invalid_params(self):
        self.assertEqual(self.client.get(URL, {"platform": "web", "build": 1}).status_code, 400)
        self.assertEqual(self.client.get(URL, {"platform": "ios"}).status_code, 400)


class FlagReachTests(TestCase):
    def test_estimate_uses_check_ins(self):
        now = timezone.now()
        for i in range(40):
            AppVersionCheckIn.objects.create(platform="android" if i < 30 else "ios",
                                             rollout_key_hash=f"{i:064d}", build_number=20, last_seen_at=now)
        flag = make_flag(pct=10, platform="android")
        self.assertEqual(flag_reach(flag), {"eligible": 30, "estimated": 3, "testers": 0, "pct": 10})
        flag = apply_flag_action(flag, "disable")
        self.assertEqual(flag_reach(flag)["estimated"], 0)


@override_settings(ALLOWED_HOSTS=["*"])
class FlagAdminTests(TestCase):
    def setUp(self):
        self.admin = make_superuser()
        self.client.force_login(self.admin)

    def quick(self, flag, action, percentage=None):
        data = {"action": action, "next": "/admin/app_releases/featureflag/"}
        if percentage is not None:
            data["percentage"] = percentage
        return self.client.post(f"/admin/app_releases/featureflag/{flag.pk}/quick/", data, follow=True)

    def test_pages_render(self):
        flag = make_flag(pct=25, payload={"variant": "b"}, target_roles=["controller"])
        changelist = self.client.get("/admin/app_releases/featureflag/")
        self.assertContains(changelist, 'role="switch"')
        self.assertContains(changelist, "data-ff-percentage=\"50\"")
        self.assertContains(changelist, "rr-progress")
        change = self.client.get(f"/admin/app_releases/featureflag/{flag.pk}/change/")
        self.assertContains(change, "Аварийный выключатель")
        self.assertContains(change, "&quot;variant&quot;")
        self.assertEqual(self.client.get("/admin/app_releases/featureflag/add/").status_code, 200)

    def test_quick_actions_with_audit(self):
        flag = make_flag(pct=5, enabled=False)
        self.quick(flag, "enable")
        self.quick(flag, "set_percentage", 25)
        response = self.quick(flag, "disable")
        self.assertContains(response, "Выключен")
        flag.refresh_from_db()
        self.assertEqual((flag.is_enabled, flag.rollout_percentage), (False, 25))
        self.assertIsNotNone(flag.disabled_at)
        actions = list(FeatureFlagEvent.objects.filter(flag=flag).order_by("created_at").values_list("action", flat=True))
        self.assertEqual(actions, ["enable", "set_percentage", "disable"])
        response = self.quick(flag, "set_percentage", 101)
        self.assertContains(response, "от 0 до 100")

    def test_quick_requires_post_and_permission(self):
        flag = make_flag()
        self.assertEqual(self.client.get(f"/admin/app_releases/featureflag/{flag.pk}/quick/").status_code, 405)
        self.client.force_login(make_tenant())
        self.client.post(f"/admin/app_releases/featureflag/{flag.pk}/quick/", {"action": "disable"})
        flag.refresh_from_db()
        self.assertTrue(flag.is_enabled)

    def test_unsafe_next_is_ignored(self):
        flag = make_flag()
        response = self.client.post(f"/admin/app_releases/featureflag/{flag.pk}/quick/",
                                    {"action": "disable", "next": "https://evil.example/"})
        self.assertEqual(response["Location"], "/admin/app_releases/featureflag/")

    def test_create_via_form_logs_and_validates(self):
        response = self.client.post("/admin/app_releases/featureflag/add/", {
            "name": "Новый экран оплаты", "key": "new_payment_screen", "rollout_percentage": "5",
            "platform": "all", "payload": '{"variant": "b"}', "target_roles": ["tenant"],
        })
        self.assertEqual(response.status_code, 302, response.content[:3000])
        flag = FeatureFlag.objects.get(key="new_payment_screen")
        self.assertEqual((flag.payload, flag.target_roles, flag.created_by), ({"variant": "b"}, ["tenant"], self.admin))
        self.assertTrue(flag.events.filter(action="create").exists())
        bad = self.client.post("/admin/app_releases/featureflag/add/", {
            "name": "x", "key": "Bad Key", "rollout_percentage": "5", "platform": "all",
            "min_build": "20", "max_build": "10",
        })
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, "латиница")

    def test_check_user_tool(self):
        tenant = make_tenant(phone="+996700000077")
        flag = make_flag(pct=50)
        dark = make_flag("dark_map", enabled=False)
        response = self.client.get("/admin/app_releases/featureflag/check-user/",
                                   {"q": "+996 700 000 077", "platform": "android", "build": "20"})
        self.assertEqual(response.status_code, 200)
        rows = {row["flag"].key: row["result"] for row in response.context["rows"]}
        self.assertEqual(rows[flag.key].bucket, flag_bucket(flag.key, str(tenant.pk)))
        self.assertEqual(rows[flag.key].value, rows[flag.key].bucket < 50)
        self.assertFalse(rows[dark.key].value)
        self.assertEqual(response.context["subject_user"], tenant)
        # Неизвестная строка — это device_id.
        response = self.client.get("/admin/app_releases/featureflag/check-user/", {"q": "abc-device"})
        self.assertIsNone(response.context["subject_user"])
        self.assertEqual(response.context["rollout_key"], "abc-device")
        self.assertContains(response, "сборка неизвестна")


class ReviewRegressionTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_invalid_token_is_401_on_features(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer nope")
        self.assertEqual(client.get(URL, {"platform": "android", "build": 1}).status_code, 401)

    def test_device_id_and_build_validated_on_features(self):
        client = APIClient()
        self.assertEqual(client.get(URL, {"platform": "android", "build": 1, "device_id": "x"}).status_code, 400)
        self.assertEqual(client.get(URL, {"platform": "android", "build": 2 ** 31}).status_code, 400)

    def test_payload_must_be_object(self):
        from django.core.exceptions import ValidationError

        for bad in ("b", [1, 2], 5, True):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                FeatureFlag(key="pay_flag", name="x", payload=bad).full_clean()
        FeatureFlag(key="pay_flag", name="x", payload={"variant": "b"}).full_clean()
        FeatureFlag(key="pay_flag", name="x", payload=None).full_clean()


@override_settings(ALLOWED_HOSTS=["*"])
class FlagAdminReviewTests(TestCase):
    def setUp(self):
        self.admin = make_superuser()
        self.client.force_login(self.admin)

    def test_stale_form_cannot_undo_kill_switch(self):
        flag = make_flag(pct=50, enabled=True)
        apply_flag_action(flag, "disable", user=self.admin)
        # Форма, открытая до выключения, всё ещё содержит is_enabled=on и 50%.
        stale = {"name": "Переименован", "is_enabled": "on", "rollout_percentage": "80",
                 "platform": "all", "payload": ""}
        response = self.client.post(f"/admin/app_releases/featureflag/{flag.pk}/change/", stale)
        self.assertEqual(response.status_code, 302, response.content[:2000])
        flag.refresh_from_db()
        self.assertEqual((flag.name, flag.is_enabled, flag.rollout_percentage), ("Переименован", False, 50))

    def test_admin_rejects_non_object_payload(self):
        response = self.client.post("/admin/app_releases/featureflag/add/", {
            "name": "x", "key": "list_payload", "rollout_percentage": "5", "platform": "all", "payload": "[1, 2]",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "JSON-объектом")

    def _count_list_queries(self) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get("/admin/app_releases/featureflag/").status_code, 200)
        return len(ctx.captured_queries)

    def test_changelist_reach_is_not_per_row(self):
        now = timezone.now()
        for i in range(5):
            AppVersionCheckIn.objects.create(platform="android", rollout_key_hash=f"{i:064d}", build_number=20,
                                             last_seen_at=now)
        for i in range(3):
            make_flag(f"flag_{i}", pct=20 * (i + 1))
        with_three = self._count_list_queries()
        for i in range(3, 8):
            make_flag(f"flag_{i}", pct=10, allowlisted_device_ids=U(f"t{i}"))
        # Охват и число тестировщиков считаются агрегатами: строки не добавляют запросов.
        self.assertEqual(self._count_list_queries(), with_three)

    def test_check_user_requires_view_user_permission(self):
        from django.contrib.auth.models import Permission

        staff = make_admin(phone="+996700000444")
        staff.user_permissions.add(*Permission.objects.filter(codename__in=["view_featureflag", "change_featureflag"]))
        self.client.force_login(staff)
        self.assertEqual(self.client.get("/admin/app_releases/featureflag/check-user/", {"q": "x"}).status_code, 403)
        self.assertNotContains(self.client.get("/admin/app_releases/featureflag/"), "check-user")
        staff.user_permissions.add(Permission.objects.get(codename="view_user"))
        self.assertEqual(self.client.get("/admin/app_releases/featureflag/check-user/", {"q": "x"}).status_code, 200)
