import uuid

from django.core.cache import cache
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.core.testing import make_controller, make_superuser, make_tenant

from .models import AppRelease, AppReleaseEvent, AppVersionCheckIn, Platform, ReleaseStatus
from .services import RolloutError, RolloutSubject, resolve_update, rollout_bucket, set_percentage, transition

URL = "/api/v1/app/version-check/"
CONTRACT_KEYS = {
    "update_available",
    "force_update",
    "latest_version",
    "latest_build",
    "min_supported_build",
    "store_url",
    "release_notes",
    "rollout_percentage",
}
PLAY = "https://play.google.com/store/apps/details?id=kg.oshbazar.electro"
APPSTORE = "https://apps.apple.com/app/id000000"


def make_release(build=13, version=None, platform=Platform.ANDROID, status=ReleaseStatus.ROLLING_OUT, pct=0, **extra):
    extra.setdefault("store_url", PLAY if platform == Platform.ANDROID else APPSTORE)
    extra.setdefault("release_notes", f"Что нового в сборке {build}")
    return AppRelease.objects.create(
        platform=platform,
        version=version or f"1.{build}.0",
        build_number=build,
        status=status,
        rollout_percentage=pct,
        **extra,
    )


def U(name) -> str:
    """Детерминированный UUID-device_id для тестов."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"device:{name}"))


def keys_in_rollout(release, n=400):
    return {f"dev-{i}" for i in range(n) if rollout_bucket(release.pk, f"dev-{i}") < release.rollout_percentage}


def device_in(release, inside=True):
    for i in range(5000):
        key = U(f"probe-{i}")
        if (rollout_bucket(release.pk, key) < release.rollout_percentage) == inside:
            return key
    raise AssertionError("no key found")


class BucketingTests(TestCase):
    def test_bucket_is_deterministic_and_in_range(self):
        release = make_release()
        buckets = [rollout_bucket(release.pk, "device-42") for _ in range(5)]
        self.assertEqual(len(set(buckets)), 1)
        self.assertTrue(0 <= buckets[0] < 100)
        # Формула из контракта.
        import hashlib

        expected = int(hashlib.sha256(f"{release.pk}:device-42".encode()).hexdigest(), 16) % 100
        self.assertEqual(buckets[0], expected)

    def test_bucket_differs_between_releases(self):
        a, b = make_release(build=13), make_release(build=14)
        diffs = sum(rollout_bucket(a.pk, f"k{i}") != rollout_bucket(b.pk, f"k{i}") for i in range(100))
        self.assertGreater(diffs, 80)

    def test_raising_percentage_never_drops_a_user(self):
        release = make_release(pct=1)
        previous = keys_in_rollout(release)
        for pct in (5, 10, 25, 50, 75, 100):
            release = set_percentage(release, pct)
            current = keys_in_rollout(release)
            self.assertTrue(previous <= current, f"users dropped when going to {pct}%")
            previous = current
        self.assertEqual(len(previous), 400)

    def test_raising_percentage_via_resolver_is_monotonic(self):
        release = make_release(pct=10)
        subjects = [RolloutSubject(device_id=f"d{i}") for i in range(300)]
        got = {s.device_id for s in subjects if resolve_update("android", 12, s).update_available}
        set_percentage(release, 30)
        got_after = {s.device_id for s in subjects if resolve_update("android", 12, s).update_available}
        self.assertTrue(got <= got_after)
        self.assertGreater(len(got_after), len(got))

    def test_distribution_is_roughly_uniform(self):
        release = make_release(pct=25)
        share = len(keys_in_rollout(release, n=4000)) / 4000
        self.assertAlmostEqual(share, 0.25, delta=0.03)


class ResolveTests(TestCase):
    def test_completed_release_reaches_everyone(self):
        make_release(build=12, status=ReleaseStatus.COMPLETED, pct=100)
        decision = resolve_update("android", 11, RolloutSubject(device_id="x"))
        self.assertTrue(decision.update_available)
        self.assertEqual(decision.release.build_number, 12)

    def test_falls_back_to_older_completed_release(self):
        make_release(build=12, status=ReleaseStatus.COMPLETED, pct=100)
        newest = make_release(build=13, pct=10)
        outsider = device_in(newest, inside=False)
        insider = device_in(newest, inside=True)
        self.assertEqual(resolve_update("android", 11, RolloutSubject(device_id=outsider)).release.build_number, 12)
        self.assertEqual(resolve_update("android", 11, RolloutSubject(device_id=insider)).release.build_number, 13)
        # Уже на 12-й и не в раскатке 13-й — обновления нет.
        self.assertFalse(resolve_update("android", 12, RolloutSubject(device_id=outsider)).update_available)

    def test_paused_draft_and_rolled_back_are_ignored(self):
        make_release(build=13, status=ReleaseStatus.PAUSED, pct=100)
        make_release(build=14, status=ReleaseStatus.ROLLED_BACK, pct=100)
        make_release(build=15, status=ReleaseStatus.DRAFT, pct=100)
        self.assertFalse(resolve_update("android", 12, RolloutSubject(device_id="x")).update_available)

    def test_anonymous_without_device_id_gets_only_full_rollouts(self):
        make_release(build=13, pct=99)
        self.assertFalse(resolve_update("android", 12, RolloutSubject()).update_available)

    def test_role_targeting(self):
        make_release(build=13, pct=100, target_roles=["controller"])
        self.assertTrue(resolve_update("android", 12, RolloutSubject(user=make_controller())).update_available)
        self.assertFalse(resolve_update("android", 12, RolloutSubject(user=make_tenant())).update_available)
        self.assertFalse(resolve_update("android", 12, RolloutSubject(device_id="anon")).update_available)

    def test_min_supported_build_picks_lowest_satisfying_release(self):
        make_release(build=12, status=ReleaseStatus.COMPLETED, pct=100, min_supported_build=12)
        newest = make_release(build=14, pct=5)
        outsider = device_in(newest, inside=False)
        decision = resolve_update("android", 10, RolloutSubject(device_id=outsider))
        self.assertTrue(decision.force_update)
        self.assertEqual(decision.release.build_number, 12)


@override_settings(APP_VERSION_CHECK_THROTTLE_RATE="1000/min")
class EndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def check(self, build=12, platform="android", device_id=U("dev-1"), **extra):
        params = {"platform": platform, "build": build, "version": "1.2.0", "device_id": device_id, **extra}
        response = self.client.get(URL, params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_response_shape_matches_contract(self):
        make_release(build=13, version="1.3.0", status=ReleaseStatus.ROLLING_OUT, pct=100,
                     min_supported_build=10, release_notes="Что нового: ...")
        data = self.check()
        self.assertEqual(set(data), CONTRACT_KEYS)
        self.assertEqual(
            data,
            {
                "update_available": True,
                "force_update": False,
                "latest_version": "1.3.0",
                "latest_build": 13,
                "min_supported_build": 10,
                "store_url": PLAY,
                "release_notes": "Что нового: ...",
                "rollout_percentage": 100,
            },
        )

    def test_no_update_shape(self):
        data = self.check()
        self.assertEqual(set(data), CONTRACT_KEYS)
        self.assertIs(data["update_available"], False)
        self.assertIs(data["force_update"], False)
        self.assertIsNone(data["rollout_percentage"])
        self.assertIsNone(data["latest_build"])

    def test_up_to_date_client_gets_no_update(self):
        make_release(build=13, status=ReleaseStatus.COMPLETED, pct=100)
        self.assertFalse(self.check(build=13)["update_available"])

    def test_force_update_via_min_supported_ignores_rollout(self):
        release = make_release(build=13, pct=1, min_supported_build=13)
        outsider = device_in(release, inside=False)
        data = self.check(build=12, device_id=outsider)
        self.assertTrue(data["update_available"])
        self.assertTrue(data["force_update"])
        self.assertEqual(data["latest_build"], 13)
        self.assertEqual(data["min_supported_build"], 13)

    def test_no_force_when_above_min_supported(self):
        make_release(build=13, pct=100, min_supported_build=10)
        self.assertFalse(self.check(build=12)["force_update"])

    def test_mandatory_release(self):
        release = make_release(build=13, pct=30, is_mandatory=True)
        inside, outside = device_in(release, True), device_in(release, False)
        data = self.check(device_id=inside)
        self.assertTrue(data["force_update"])
        self.assertEqual(data["rollout_percentage"], 30)
        self.assertFalse(self.check(device_id=outside)["update_available"])

    def test_paused_and_rolled_back_ignored(self):
        make_release(build=13, status=ReleaseStatus.PAUSED, pct=100)
        make_release(build=14, status=ReleaseStatus.ROLLED_BACK, pct=100, min_supported_build=14)
        data = self.check()
        self.assertFalse(data["update_available"])
        self.assertFalse(data["force_update"])

    def test_device_allowlist(self):
        make_release(build=13, pct=0, allowlisted_device_ids=f"{U('tester')}\n{U('another')}")
        self.assertTrue(self.check(device_id=U("tester"))["update_available"])
        self.assertTrue(self.check(device_id=U("tester").upper())["update_available"])  # нормализация UUID
        self.assertFalse(self.check(device_id=U("random"))["update_available"])

    def test_user_allowlist_requires_auth(self):
        tester = make_tenant()
        release = make_release(build=13, pct=0)
        release.allowlisted_users.add(tester)
        self.assertFalse(self.check(device_id=U("d"))["update_available"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(tester)}")
        self.assertTrue(self.check(device_id=U("d"))["update_available"])

    def test_authenticated_uses_user_id_as_key(self):
        user = make_tenant()
        release = make_release(build=13, pct=50)
        expected = rollout_bucket(release.pk, str(user.pk)) < 50
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
        # Ключ — пользователь, поэтому device_id не влияет на результат.
        results = {self.check(device_id=U(f"dev-{i}"))["update_available"] for i in range(10)}
        self.assertEqual(results, {expected})
        check_in = AppVersionCheckIn.objects.get()
        self.assertEqual(check_in.user, user)
        self.assertEqual(check_in.check_count, 10)

    def test_anonymous_uses_device_id_and_upserts(self):
        make_release(build=13, pct=100)
        self.check(device_id=U("dev-a"))
        self.check(device_id=U("dev-a"), build=13)
        self.check(device_id=U("dev-b"))
        self.assertEqual(AppVersionCheckIn.objects.count(), 2)
        row = AppVersionCheckIn.objects.get(check_count=2)
        self.assertIsNone(row.user)
        self.assertEqual(row.build_number, 13)
        self.assertNotIn(U("dev-a"), row.rollout_key_hash)

    def test_no_authorization_header_is_anonymous(self):
        make_release(build=13, pct=100)
        self.assertTrue(self.check()["update_available"])
        self.assertIsNone(AppVersionCheckIn.objects.get().user)

    def test_invalid_or_expired_token_is_401(self):
        make_release(build=13, pct=100)
        params = {"platform": "android", "build": 12, "device_id": U("x")}
        self.client.credentials(HTTP_AUTHORIZATION="Bearer broken.token.value")
        self.assertEqual(self.client.get(URL, params).status_code, 401)
        expired = AccessToken.for_user(make_tenant())
        expired.set_exp(lifetime=-timezone.timedelta(minutes=1))
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {expired}")
        self.assertEqual(self.client.get(URL, params).status_code, 401)

    def test_platform_separation(self):
        make_release(build=13, platform=Platform.IOS, pct=100)
        self.assertFalse(self.check(platform="android")["update_available"])
        data = self.check(platform="ios")
        self.assertTrue(data["update_available"])
        self.assertEqual(data["store_url"], APPSTORE)

    def test_kyrgyz_release_notes(self):
        make_release(build=13, pct=100, release_notes="Рус", release_notes_ky="Кыр")
        self.assertEqual(self.check(lang="ky")["release_notes"], "Кыр")
        self.assertEqual(self.check()["release_notes"], "Рус")

    def test_validation_errors(self):
        self.assertEqual(self.client.get(URL, {"platform": "windows", "build": 1}).status_code, 400)
        self.assertEqual(self.client.get(URL, {"platform": "android"}).status_code, 400)
        self.assertEqual(self.client.get(URL, {"platform": "android", "build": "abc"}).status_code, 400)

    @override_settings(APP_VERSION_CHECK_THROTTLE_RATE="3/min")
    def test_anonymous_throttled_per_ip_not_device(self):
        for i in range(3):
            self.check(device_id=U(f"spam-{i}"))
        # Смена device_id не обходит лимит: ключ анонимного троттлинга — IP.
        response = self.client.get(URL, {"platform": "android", "build": 1, "device_id": U("fresh")})
        self.assertEqual(response.status_code, 429)
        # Авторизованный пользователь считается отдельно.
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(make_tenant())}")
        self.check()

    def test_device_id_must_be_uuid(self):
        response = self.client.get(URL, {"platform": "android", "build": 1, "device_id": "not-a-uuid"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errors"][0]["field"], "device_id")

    def test_build_above_int32_is_400(self):
        response = self.client.get(URL, {"platform": "android", "build": 99999999999, "device_id": U("b")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get(URL, {"platform": "android", "build": 2147483647}).status_code, 200)

    def test_role_targeted_min_supported_does_not_force_others(self):
        make_release(build=12, status=ReleaseStatus.COMPLETED, pct=100)
        make_release(build=13, pct=100, min_supported_build=13, target_roles=["controller"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(make_tenant())}")
        data = self.check(build=12)
        self.assertFalse(data["force_update"])
        self.assertFalse(data["update_available"])
        self.assertIsNone(data["min_supported_build"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(make_controller())}")
        data = self.check(build=12)
        self.assertTrue(data["force_update"])
        self.assertEqual(data["latest_build"], 13)


class TransitionTests(TestCase):
    def setUp(self):
        self.admin = make_superuser()

    def test_start_defaults_to_five_percent_and_logs(self):
        release = make_release(status=ReleaseStatus.DRAFT)
        release = transition(release, "start", user=self.admin)
        self.assertEqual(release.status, ReleaseStatus.ROLLING_OUT)
        self.assertEqual(release.rollout_percentage, 5)
        self.assertIsNotNone(release.started_at)
        event = AppReleaseEvent.objects.get(release=release)
        self.assertEqual((event.action, event.from_status, event.to_percentage), ("start", "draft", 5))

    def test_percentage_cannot_go_down(self):
        release = make_release(pct=25)
        with self.assertRaises(RolloutError):
            set_percentage(release, 10)
        release.refresh_from_db()
        self.assertEqual(release.rollout_percentage, 25)
        paused = make_release(build=14, status=ReleaseStatus.PAUSED, pct=25)
        with self.assertRaises(RolloutError):
            set_percentage(paused, 5)

    def test_model_clean_blocks_decrease(self):
        release = make_release(pct=25)
        release.rollout_percentage = 10
        with self.assertRaises(ValidationError):
            release.full_clean()

    def test_hundred_percent_completes(self):
        release = set_percentage(make_release(pct=50), 100)
        self.assertEqual(release.status, ReleaseStatus.COMPLETED)
        self.assertIsNotNone(release.completed_at)

    def test_lifecycle(self):
        release = make_release(status=ReleaseStatus.DRAFT)
        release = transition(release, "start", percentage=10)
        release = transition(release, "pause")
        self.assertEqual(release.status, ReleaseStatus.PAUSED)
        with self.assertRaises(RolloutError):
            transition(release, "pause")
        release = transition(release, "resume")
        release = transition(release, "complete")
        self.assertEqual((release.status, release.rollout_percentage), (ReleaseStatus.COMPLETED, 100))
        release = transition(release, "roll_back")
        self.assertEqual(release.status, ReleaseStatus.ROLLED_BACK)
        with self.assertRaises(RolloutError):
            transition(release, "resume")
        self.assertEqual(release.events.count(), 5)

    def test_build_must_increase(self):
        make_release(build=13)
        with self.assertRaises(ValidationError):
            AppRelease(platform="android", version="1.2.0", build_number=12, store_url=PLAY).full_clean()
        # Другая платформа — независимая нумерация.
        AppRelease(platform="ios", version="1.2.0", build_number=12, store_url=APPSTORE).full_clean()

    def test_min_supported_not_above_build_and_semver(self):
        with self.assertRaises(ValidationError):
            AppRelease(platform="android", version="1.3.0", build_number=13, min_supported_build=14,
                       store_url=PLAY).full_clean()
        with self.assertRaises(ValidationError):
            AppRelease(platform="android", version="v1.3", build_number=13, store_url=PLAY).full_clean()


@override_settings(ALLOWED_HOSTS=["*"])
class AdminTests(TestCase):
    def setUp(self):
        self.admin = make_superuser()
        self.client.force_login(self.admin)

    def url(self, release, suffix=""):
        return f"/admin/app_releases/apprelease/{release.pk}/{suffix}"

    def post_action(self, release, action, percentage=None):
        data = {"action": action}
        if percentage is not None:
            data["percentage"] = percentage
        return self.client.post(self.url(release, "rollout/"), data, follow=True)

    def test_pages_render(self):
        release = make_release(pct=25, min_supported_build=10, is_mandatory=True)
        AppVersionCheckIn.objects.create(platform="android", rollout_key_hash="a" * 64, build_number=12,
                                         version="1.2.0", last_seen_at=release.created_at)
        changelist = self.client.get("/admin/app_releases/apprelease/")
        self.assertContains(changelist, "rr-progress")
        self.assertContains(changelist, "Раскатывается")
        change = self.client.get(self.url(release, "change/"))
        self.assertContains(change, "data-rr-panel")
        self.assertContains(change, "rr-chart-data")
        self.assertContains(change, "Приостановить")
        self.assertEqual(self.client.get("/admin/app_releases/apprelease/add/").status_code, 200)
        self.assertEqual(self.client.get("/admin/app_releases/appversioncheckin/").status_code, 200)

    def test_rollout_actions_flow(self):
        release = make_release(status=ReleaseStatus.DRAFT)
        response = self.post_action(release, "start", 10)
        self.assertContains(response, "Раскатка начата")
        release.refresh_from_db()
        self.assertEqual((release.status, release.rollout_percentage), (ReleaseStatus.ROLLING_OUT, 10))

        self.post_action(release, "set_percentage", 25)
        release.refresh_from_db()
        self.assertEqual(release.rollout_percentage, 25)

        response = self.post_action(release, "set_percentage", 5)
        self.assertContains(response, "нельзя уменьшить")
        release.refresh_from_db()
        self.assertEqual(release.rollout_percentage, 25)

        self.post_action(release, "pause")
        release.refresh_from_db()
        self.assertEqual(release.status, ReleaseStatus.PAUSED)
        self.post_action(release, "resume")
        self.post_action(release, "roll_back")
        release.refresh_from_db()
        self.assertEqual(release.status, ReleaseStatus.ROLLED_BACK)
        self.assertEqual(release.events.count(), 5)
        self.assertEqual(release.events.filter(user=self.admin).count(), 5)

    def test_rollout_view_requires_post_and_staff(self):
        release = make_release()
        self.assertEqual(self.client.get(self.url(release, "rollout/")).status_code, 405)
        self.client.force_login(make_tenant())
        response = self.client.post(self.url(release, "rollout/"), {"action": "pause"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])
        release.refresh_from_db()
        self.assertEqual(release.status, ReleaseStatus.ROLLING_OUT)

    def test_add_form_rejects_lower_build_and_sets_creator(self):
        make_release(build=13)
        base = {"platform": "android", "version": "1.4.0", "store_url": PLAY, "rollout_percentage": "0"}
        response = self.client.post("/admin/app_releases/apprelease/add/", {**base, "build_number": "12"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "больше предыдущего")
        response = self.client.post(
            "/admin/app_releases/apprelease/add/", {**base, "build_number": "14", "target_roles": ["controller"]}
        )
        self.assertEqual(response.status_code, 302, response.content[:3000])
        created = AppRelease.objects.get(build_number=14)
        self.assertEqual(created.created_by, self.admin)
        self.assertEqual(created.status, ReleaseStatus.DRAFT)
        self.assertEqual(created.target_roles, ["controller"])

    def test_bulk_pause_action(self):
        a, b = make_release(build=13, pct=10), make_release(build=14, status=ReleaseStatus.DRAFT)
        self.client.post(
            "/admin/app_releases/apprelease/",
            {"action": "pause_selected", "_selected_action": [str(a.pk), str(b.pk)]},
        )
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.status, b.status), (ReleaseStatus.PAUSED, ReleaseStatus.DRAFT))


class PruneCommandTests(TestCase):
    def test_prunes_old_check_ins(self):
        now = timezone.now()
        AppVersionCheckIn.objects.create(platform="android", rollout_key_hash="a" * 64, build_number=1,
                                         last_seen_at=now - timezone.timedelta(days=31))
        AppVersionCheckIn.objects.create(platform="android", rollout_key_hash="b" * 64, build_number=1,
                                         last_seen_at=now - timezone.timedelta(days=2))
        call_command("prune_version_checkins", "--days", "30", stdout=open("/dev/null", "w"))
        self.assertEqual(list(AppVersionCheckIn.objects.values_list("rollout_key_hash", flat=True)), ["b" * 64])


@override_settings(ALLOWED_HOSTS=["*"])
class AdminPermissionTests(TestCase):
    def test_pause_action_requires_change_permission(self):
        from django.contrib.auth.models import Permission

        viewer = make_tenant(phone="+996700000555", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_apprelease"))
        self.client.force_login(viewer)
        release = make_release(pct=10)
        response = self.client.get("/admin/app_releases/apprelease/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "pause_selected")
        self.client.post("/admin/app_releases/apprelease/",
                         {"action": "pause_selected", "_selected_action": [str(release.pk)]})
        release.refresh_from_db()
        self.assertEqual(release.status, ReleaseStatus.ROLLING_OUT)

    def test_status_and_percentage_not_editable_via_form(self):
        self.client.force_login(make_superuser())
        release = make_release(pct=25)
        data = {"version": "1.13.1", "store_url": PLAY, "rollout_percentage": "5", "status": "draft",
                "min_supported_build": "", "release_notes": "x"}
        response = self.client.post(f"/admin/app_releases/apprelease/{release.pk}/change/", data)
        self.assertEqual(response.status_code, 302, response.content[:2000])
        release.refresh_from_db()
        self.assertEqual((release.version, release.status, release.rollout_percentage),
                         ("1.13.1", ReleaseStatus.ROLLING_OUT, 25))
