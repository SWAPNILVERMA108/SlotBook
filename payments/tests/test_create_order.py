"""DAY 10: POST /api/payments/create-order/"""

from decimal import Decimal
from unittest import mock

import requests
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from bookings.models import BookingStatus
from payments import services
from payments.models import Payment

from .helpers import KEY_ID, PaymentFixtureMixin, with_razorpay


@with_razorpay
class CreateOrderTests(PaymentFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.fake = self.fake_razorpay()
        self.api = APIClient()
        self.api.force_authenticate(self.alice)
        self.url = reverse("payment-create-order")
        self.booking = self.make_booking()

    def order(self, booking_id=None, client=None):
        return (client or self.api).post(
            self.url, {"booking_id": booking_id or self.booking.id}, format="json"
        )

    def test_requires_login(self):
        self.assertEqual(self.order(client=APIClient()).status_code, 401)

    def test_creates_order_with_amount_in_paise_and_saves_payment(self):
        response = self.order()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["amount"], 50000)  # 500.00 rupees = 50000 paise
        self.assertEqual(data["currency"], "INR")
        self.assertEqual(data["key_id"], KEY_ID)
        self.assertEqual(data["booking_id"], self.booking.id)
        self.assertFalse(data["free"])

        method, path, body = self.fake.calls[0]
        self.assertEqual((method, path), ("POST", "/v1/orders"))
        self.assertEqual(body["amount"], 50000)
        self.assertEqual(body["receipt"], f"booking_{self.booking.id}")

        payment = Payment.objects.get()
        self.assertEqual(payment.razorpay_order_id, data["order_id"])
        self.assertEqual(payment.amount, Decimal("500.00"))
        self.assertEqual(payment.status, "CREATED")

    def test_secret_key_is_never_sent_to_the_browser(self):
        self.assertNotIn("dummy_secret", self.order().content.decode())

    def test_rupees_to_paise_is_exact(self):
        self.assertEqual(
            services.rupees_to_paise(Decimal("19.99")), 1999
        )  # float would give 1998.99999
        self.assertEqual(services.rupees_to_paise(Decimal("1234.56")), 123456)
        self.assertEqual(services.rupees_to_paise(Decimal("0.10")), 10)
        self.assertEqual(services.rupees_to_paise(Decimal("0.00")), 0)

    def test_second_click_reuses_the_open_order(self):
        first = self.order().json()
        second = self.order().json()
        self.assertEqual(first["order_id"], second["order_id"])
        self.assertEqual(len(self.fake.calls_to("/v1/orders")), 1)  # Razorpay was asked only once
        self.assertEqual(Payment.objects.count(), 1)

    def test_other_users_booking_is_404(self):
        response = self.order(client=self._client_for(self.bob))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.fake.calls, [])

    def _client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_unknown_booking_is_404(self):
        self.assertEqual(self.order(booking_id=999999).status_code, 404)

    def test_bad_input_is_400(self):
        for body in ({}, {"booking_id": "abc"}, {"booking_id": 0}):
            self.assertEqual(self.api.post(self.url, body, format="json").status_code, 400, body)

    def test_confirmed_cancelled_and_expired_bookings_cannot_be_paid(self):
        for status in (BookingStatus.CONFIRMED, BookingStatus.CANCELLED, BookingStatus.EXPIRED):
            booking = self.make_booking(
                slot=self.make_slot(hours_from_now=30 + len(status)), status=status
            )
            self.assertEqual(self.order(booking.id).status_code, 409, status)
        self.assertEqual(self.fake.calls, [])

    def test_pending_booking_whose_hold_ended_cannot_be_paid(self):
        late = self.make_booking(slot=self.make_slot(hours_from_now=40), minutes_left=-1)
        response = self.order(late.id)
        self.assertEqual(response.status_code, 409)
        self.assertIn("hold", response.json()["detail"])

    def test_razorpay_unreachable_gives_502_and_saves_nothing(self):
        self.fake.error = requests.exceptions.ConnectTimeout("simulated")
        self.assertEqual(self.order().status_code, 502)
        self.assertEqual(Payment.objects.count(), 0)

    def test_razorpay_error_answer_gives_502_and_saves_nothing(self):
        self.fake.error_status = 400
        self.assertEqual(self.order().status_code, 502)
        self.assertEqual(Payment.objects.count(), 0)

    @override_settings(RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET="")
    def test_missing_keys_give_503(self):
        self.assertEqual(self.order().status_code, 503)

    def test_free_slot_is_confirmed_without_payment(self):
        free = self.make_booking(slot=self.make_slot(price=Decimal("0.00"), hours_from_now=50))
        response = self.order(free.id)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["free"])
        free.refresh_from_db()
        self.assertEqual(free.status, BookingStatus.CONFIRMED)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(Payment.objects.count(), 0)


class TimeoutSessionTests(TestCase):
    def test_every_razorpay_call_has_a_timeout(self):
        session = services.make_session()
        with mock.patch.object(requests.Session, "request", return_value="ok") as sent:
            session.request("POST", "https://api.razorpay.com/v1/orders")
        self.assertEqual(sent.call_args.kwargs["timeout"], services.RAZORPAY_TIMEOUT_SECONDS)

    def test_a_caller_can_still_choose_a_different_timeout(self):
        session = services.make_session()
        with mock.patch.object(requests.Session, "request", return_value="ok") as sent:
            session.request("POST", "https://x", timeout=3)
        self.assertEqual(sent.call_args.kwargs["timeout"], 3)
