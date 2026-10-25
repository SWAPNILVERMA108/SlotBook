"""Shared tools for the payment tests.

FakeRazorpaySession pretends to be Razorpay's SERVER. We do NOT mock the Razorpay library:
the real library runs, builds the real URLs and bodies, and talks to this fake server. So the
tests also prove that we call Razorpay the right way (right endpoint, amount in paise, timeout).
"""

import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from bookings.models import Booking, BookingStatus
from payments.models import Payment
from venues.models import Slot, Venue

User = get_user_model()

KEY_ID = "rzp_test_dummy"
KEY_SECRET = "dummy_secret"
WEBHOOK_SECRET = "dummy_webhook_secret"

RAZORPAY_SETTINGS = dict(
    RAZORPAY_KEY_ID=KEY_ID,
    RAZORPAY_KEY_SECRET=KEY_SECRET,
    RAZORPAY_WEBHOOK_SECRET=WEBHOOK_SECRET,
)


class FakeRazorpaySession(requests.Session):
    """A fake Razorpay server. Records every call in self.calls as (METHOD, path, body)."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.error = None  # set to an exception to simulate "Razorpay is unreachable"
        self.error_status = None  # set to e.g. 400 to simulate Razorpay refusing the request
        self.timeouts = []
        self._counter = 0

    def request(self, method, url, **kwargs):
        path = url.replace("https://api.razorpay.com", "")
        body = json.loads(kwargs["data"]) if kwargs.get("data") else {}
        self.calls.append((method.upper(), path, body))
        self.timeouts.append(kwargs.get("timeout"))
        if self.error:
            raise self.error
        if self.error_status:
            return self._response(
                self.error_status,
                {"error": {"code": "BAD_REQUEST_ERROR", "description": "Simulated Razorpay error"}},
            )
        self._counter += 1
        if path == "/v1/orders":
            return self._response(
                200,
                {
                    "id": f"order_FAKE{self._counter}",
                    "amount": body["amount"],
                    "currency": body["currency"],
                    "receipt": body.get("receipt"),
                    "status": "created",
                },
            )
        if path.startswith("/v1/payments/") and path.endswith("/refund"):
            return self._response(
                200,
                {
                    "id": f"rfnd_FAKE{self._counter}",
                    "amount": body.get("amount"),
                    "status": "processed",
                },
            )
        return self._response(
            404, {"error": {"code": "BAD_REQUEST_ERROR", "description": "unknown path"}}
        )

    @staticmethod
    def _response(status_code, data):
        response = requests.Response()
        response.status_code = status_code
        response._content = json.dumps(data).encode()
        response.headers["Content-Type"] = "application/json"
        return response

    def calls_to(self, path_start):
        return [c for c in self.calls if c[1].startswith(path_start)]


def checkout_signature(order_id, payment_id, secret=KEY_SECRET):
    return hmac.new(
        secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()


def webhook_body(order_id, payment_id, amount_paise, event="payment.captured"):
    payload = {
        "entity": "event",
        "event": event,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount_paise,
                    "status": "failed" if event == "payment.failed" else "captured",
                    "error_description": "Bank declined",
                }
            }
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode()


def webhook_signature(body, secret=WEBHOOK_SECRET):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post_webhook(client, body, event_id="evt_1", signature=None, **extra):
    headers = {
        "HTTP_X_RAZORPAY_SIGNATURE": signature if signature is not None else webhook_signature(body)
    }
    if event_id is not None:
        headers["HTTP_X_RAZORPAY_EVENT_ID"] = event_id
    headers.update(extra)
    return client.post(
        "/api/payments/webhook/", data=body, content_type="application/json", **headers
    )


class PaymentFixtureMixin:
    """Builds: an owner, two customers, a venue, a slot (price 500.00) and helpers."""

    PRICE = Decimal("500.00")

    def make_world(self):
        self.owner = User.objects.create_user("owner")
        self.alice = User.objects.create_user("alice")
        self.bob = User.objects.create_user("bob")
        self.venue = Venue.objects.create(
            owner=self.owner, name="City Turf", address="A", city="Noida"
        )
        self.slot = self.make_slot()

    def make_slot(self, price=None, hours_from_now=24, **extra):
        start = timezone.now() + timedelta(hours=hours_from_now)
        return Slot.objects.create(
            venue=self.venue,
            start_time=start,
            end_time=start + timedelta(minutes=30),
            price=self.PRICE if price is None else price,
            **extra,
        )

    def make_booking(self, user=None, slot=None, status=BookingStatus.PENDING, minutes_left=10):
        expires = (
            timezone.now() + timedelta(minutes=minutes_left)
            if status == BookingStatus.PENDING
            else None
        )
        return Booking.objects.create(
            user=user or self.alice, slot=slot or self.slot, status=status, expires_at=expires
        )

    def make_payment(self, booking, order_id="order_T1", amount=None, **extra):
        return Payment.objects.create(
            booking=booking, razorpay_order_id=order_id, amount=amount or self.PRICE, **extra
        )

    def fake_razorpay(self):
        """Swap the real HTTP session for the fake Razorpay server. Returns the fake."""
        fake = FakeRazorpaySession()
        patcher = mock.patch("payments.services.make_session", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def capture_refund_queue(self):
        """Replace the Celery call that queues a refund. Returns the mock to inspect."""
        patcher = mock.patch("payments.tasks.process_refund.delay")
        queued = patcher.start()
        self.addCleanup(patcher.stop)
        return queued


with_razorpay = override_settings(**RAZORPAY_SETTINGS)
