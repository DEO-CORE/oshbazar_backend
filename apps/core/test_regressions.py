from datetime import date
from decimal import Decimal
from uuid import UUID
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.billing.models import BillingPeriod, Charge, LedgerEntry
from apps.billing.services import consumption_history, create_charge_for_reading, recalculate_charge, user_balance
from apps.core.testing import make_tenant, make_trade_point, make_meter, make_tariff, grant_access
from apps.integrations.clients import Http1CClient
from apps.payments.models import Payment
from apps.payments.services import initiate_payment, PaymentError, process_webhook
from apps.payments.providers.base import sign_payload
import json
from apps.readings.models import MeterReading, ReadingStatus
from apps.readings.services import previous_value_for, submit_reading
from apps.users.models import PinRecoveryRequest


@override_settings(PUSH_ENABLED=False, PAYMENT_SANDBOX_ENABLED=True)
class RegressionTests(TestCase):
    def setUp(self):
        self.user = make_tenant()
        self.point = make_trade_point()
        grant_access(self.user, self.point)
        self.meter = make_meter(self.point, initial='100')
        self.tariff = make_tariff()
        self.jan = BillingPeriod.objects.create(id=UUID(int=100), year=2026, month=1,
            starts_on=date(2026,1,1), ends_on=date(2026,1,31))
        self.feb = BillingPeriod.objects.create(id=UUID(int=1), year=2026, month=2,
            starts_on=date(2026,2,1), ends_on=date(2026,2,28))

    def reading(self, meter=None, period=None, previous='100', value='200'):
        return MeterReading.objects.create(meter=meter or self.meter, trade_point=self.point,
            period=period or self.jan, submitted_by=self.user, previous_value=previous,
            value=value, status=ReadingStatus.APPROVED)

    def test_previous_reading_uses_calendar_not_uuid(self):
        self.reading()
        self.assertEqual(previous_value_for(self.meter, self.feb), Decimal('200'))
        self.assertEqual(previous_value_for(self.meter, self.jan), Decimal('100'))

    def test_recalculation_adjusts_ledger_and_is_repeatable(self):
        charge = create_charge_for_reading(self.reading())
        self.tariff.price = Decimal('7'); self.tariff.save()
        charge = recalculate_charge(charge)
        self.assertEqual(charge.amount, Decimal('700'))
        self.assertEqual(user_balance(self.user), charge.amount)
        recalculate_charge(charge)
        self.assertEqual(user_balance(self.user), charge.amount)
        self.assertEqual(LedgerEntry.objects.filter(charge=charge).count(), 2)

    def test_paid_charge_cannot_be_recalculated(self):
        charge = create_charge_for_reading(self.reading())
        charge.paid_amount = Decimal('1'); charge.save()
        with self.assertRaises(ValueError): recalculate_charge(charge)

    def test_history_sums_meters_and_payment_status(self):
        first = create_charge_for_reading(self.reading())
        self.meter.is_active = False; self.meter.save()
        second_meter = make_meter(self.point, serial='SN-2')
        create_charge_for_reading(self.reading(meter=second_meter))
        first.paid_amount = first.amount; first.sync_status()
        row = consumption_history(self.point)[0]
        self.assertEqual(Decimal(row['amount']), Decimal('700'))
        self.assertEqual(Decimal(row['paid']), Decimal('350'))
        self.assertEqual(row['status'], 'partial')

    def test_idempotent_retry_does_not_duplicate_money(self):
        first = initiate_payment(user=self.user, amount='10', idempotency_key='retry')
        second = initiate_payment(user=self.user, amount='10', idempotency_key='retry')
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(user_balance(self.user), Decimal('-10'))

    def test_key_cannot_return_another_users_payment(self):
        initiate_payment(user=self.user, amount='10', idempotency_key='shared')
        other = make_tenant(phone='+996700000099')
        with self.assertRaises(PaymentError):
            initiate_payment(user=other, amount='10', idempotency_key='shared')
        self.assertFalse(Payment.objects.filter(user=other).exists())

    def test_key_cannot_change_amount(self):
        initiate_payment(user=self.user, amount='10', idempotency_key='same')
        with self.assertRaises(PaymentError):
            initiate_payment(user=self.user, amount='11', idempotency_key='same')

    @override_settings(PAYMENT_SANDBOX_ENABLED=False)
    def test_mock_payment_disabled_without_explicit_opt_in(self):
        with self.assertRaises(PaymentError) as ctx:
            initiate_payment(user=self.user, amount='10')
        self.assertEqual(ctx.exception.code, 'sandbox_disabled')
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(user_balance(self.user), 0)

    @override_settings(READING_PHOTO_REQUIRED=True)
    def test_reading_requires_photo(self):
        with self.assertRaises(ValidationError):
            submit_reading(user=self.user, meter=self.meter, value=Decimal('200'), period=self.jan)
        self.assertFalse(MeterReading.objects.exists())

    @override_settings(READING_PHOTO_REQUIRED=False)
    def test_optional_photo_setting_is_respected(self):
        reading = submit_reading(user=self.user, meter=self.meter, value=Decimal('200'), period=self.jan)
        self.assertEqual(reading.status, ReadingStatus.APPROVED)
        self.assertEqual(Charge.objects.count(), 1)

    def test_anonymous_recovery_has_uniform_response_and_deduplicates(self):
        client = APIClient()
        url = '/api/v1/auth/pin-recovery/'
        known = client.post(url, {'identifier':self.user.phone}, format='json')
        unknown = client.post(url, {'identifier':'+996999999999'}, format='json')
        client.post(url, {'identifier':self.user.phone}, format='json')
        self.assertEqual(known.status_code, 202)
        self.assertEqual(known.data, unknown.data)
        self.assertEqual(PinRecoveryRequest.objects.count(), 1)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_pin('1234'))

    def test_1c_accepts_json_lists_and_wrapped_items(self):
        client = Http1CClient()
        for method in (client.fetch_tenants, client.fetch_trade_points, client.fetch_meters):
            for payload in ([{'id':'1'}], {'items':[{'id':'1'}]}):
                with patch.object(client, '_request', return_value=payload):
                    self.assertEqual(method(), [{'id':'1'}])

    def test_late_bank_callback_cannot_reset_successful_payment(self):
        payment = initiate_payment(user=self.user, amount='10')
        payload = {'payment_id':str(payment.pk), 'status':'processing'}
        body = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode()
        from django.conf import settings
        process_webhook('mock', payload,
            {'X-Signature':sign_payload(payload, settings.PAYMENT_WEBHOOK_SECRET)}, body)
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'succeeded')
        self.assertEqual(user_balance(self.user), Decimal('-10'))

    def test_photo_download_requires_access(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from apps.readings.models import ReadingPhoto
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            reading = self.reading()
            photo = ReadingPhoto.objects.create(
                file=SimpleUploadedFile('meter.png', b'photo-data', content_type='image/png'),
                content_type='image/png', uploaded_by=self.user, trade_point=self.point,
                meter=self.meter, original_value=200)
            reading.photo = photo; reading.save()
            url = f'/api/v1/reading-photos/{photo.pk}/file/'
            client = APIClient()
            self.assertEqual(client.get(url).status_code, 401)
            other = make_tenant(phone='+996700000088')
            client.force_authenticate(user=other)
            self.assertEqual(client.get(url).status_code, 404)
            client.force_authenticate(user=self.user)
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), b'photo-data')
