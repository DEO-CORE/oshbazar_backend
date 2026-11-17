from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.core.testing import make_tenant
from apps.trade_points.models import Meter, TradePoint, TradePointAccess
from apps.users.models import User

ADD_URL = "/admin/trade_points/tradepoint/add/"


def inline_data(meters=(), accesses=()):
    data = {}
    for prefix, rows in (("meters", meters), ("user_accesses", accesses)):
        data.update({
            f"{prefix}-TOTAL_FORMS": str(len(rows)),
            f"{prefix}-INITIAL_FORMS": "0",
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
        })
        for i, row in enumerate(rows):
            data.update({f"{prefix}-{i}-{k}": v for k, v in row.items()})
    return data


@override_settings(ALLOWED_HOSTS=["*"])
class TradePointAdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(phone="+996700000999", pin="1234", username="root")
        self.client.force_login(self.admin)

    def test_add_page_opens(self):
        self.assertEqual(self.client.get(ADD_URL).status_code, 200)

    def test_add_point_with_meter_and_tenant_in_one_form(self):
        tenant = make_tenant()
        data = {"name": "Овощи", "block": "А", "place_number": "12", "is_active": "on"}
        data |= inline_data(
            meters=[{"serial_number": "SN-1", "initial_reading": "10", "is_primary": "on", "is_active": "on"}],
            accesses=[{"user": str(tenant.pk), "access_role": "tenant", "is_active": "on"}],
        )
        response = self.client.post(ADD_URL, data)
        self.assertEqual(response.status_code, 302, response.content[:2000])
        point = TradePoint.objects.get(code="А-12")
        self.assertTrue(point.meters.get().is_primary)
        self.assertEqual(TradePointAccess.objects.get(trade_point=point).granted_by, self.admin)

    def test_two_primary_meters_rejected_without_500(self):
        data = {"name": "X", "code": "X-1", "is_active": "on"} | inline_data(
            meters=[
                {"serial_number": "SN-A", "initial_reading": "0", "is_primary": "on", "is_active": "on"},
                {"serial_number": "SN-B", "initial_reading": "0", "is_primary": "on", "is_active": "on"},
            ]
        )
        response = self.client.post(ADD_URL, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Основным может быть только один")
        self.assertFalse(TradePoint.objects.filter(code="X-1").exists())

    def test_csv_import(self):
        tenant = make_tenant()
        tenant.inn = "12345678901234"
        tenant.save()
        csv_text = (
            "name;block;place_number;meter_serial;initial_reading;tenant_inn\n"
            "Овощи;А;12;SN-100;1520,5;12345678901234\n"
            "Ткани;Б;3;SN-101;0;\n"
            ";В;1;;;\n"
        )
        upload = SimpleUploadedFile("points.csv", csv_text.encode("utf-8-sig"), "text/csv")
        response = self.client.post("/admin/trade_points/tradepoint/import/", {"file": upload}, follow=True)
        self.assertContains(response, "Создано точек: 2")
        self.assertContains(response, "Строка 4")
        self.assertEqual(Meter.objects.get(serial_number="SN-100").initial_reading, 1520.5)
        self.assertTrue(TradePointAccess.objects.filter(user=tenant, trade_point__code="А-12").exists())
