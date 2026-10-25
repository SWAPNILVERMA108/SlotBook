"""DAY 11: POST /api/payments/webhook/"""

from unittest import mock

from django.test import Client, TestCase, override_settings

from bookings.models import BookingStatus
from payments import services
from payments.models import Payment, PaymentStatus, WebhookEvent

from .helpers import (
    PaymentFixtureMixin,
    post_webhook,
    webhook_body,
    webhook_signature,
    with_razorpay,
)


@with_razorpay
class WebhookTests(PaymentFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.queue = self.capture_refund_queue()
        self.booking = self.make_booking()
        self.payment = self.make_payment(self.booking, "order_A")
        self.client = Client()

    def captured(self, order="order_A", payment="pay_A", amount=50000, event="payment.captured"):
        return webhook_body(order, payment, amount, event)

    def reload(self):
        self.booking.refresh_from_db()
        self.payment.refresh_from_db()

    # ---- signature and basic request checks ----
    def test_valid_captured_event_confirms_the_booking(self):
        response = post_webhook(self.client, self.captured())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "confirmed"})
        self.reload()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)
        self.assertEqual(self.payment.status, PaymentStatus.PAID)
        self.assertEqual(self.payment.razorpay_payment_id, "pay_A")

    def test_event_is_recorded_without_personal_data(self):
        post_webhook(self.client, self.captured(), event_id="evt_42")
        event = WebhookEvent.objects.get()
        self.assertEqual(
            (event.event_id, event.event_type, event.result),
            ("evt_42", "payment.captured", "confirmed"),
        )

    def test_wrong_signature_is_rejected_and_nothing_changes(self):
        response = post_webhook(self.client, self.captured(), signature="0" * 64)
        self.assertEqual(response.status_code, 400)
        self.reload()
        self.assertEqual(self.booking.status, BookingStatus.PENDING)
        self.assertEqual(WebhookEvent.objects.count(), 0)

    def test_missing_signature_header_is_rejected(self):
        body = self.captured()
        response = self.client.post(
            "/api/payments/webhook/", data=body, content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_signature_made_with_another_secret_is_rejected(self):
        body = self.captured()
        response = post_webhook(
            self.client, body, signature=webhook_signature(body, secret="attacker")
        )
        self.assertEqual(response.status_code, 400)

    def test_body_changed_after_signing_is_rejected(self):
        body = self.captured(amount=50000)
        signature = webhook_signature(body)
        tampered = body.replace(b"pay_A", b"pay_Z")
        self.assertEqual(post_webhook(self.client, tampered, signature=signature).status_code, 400)

    def test_reformatted_json_breaks_the_signature(self):
        """Why we hash the RAW bytes: same JSON, different spaces = different signature."""
        body = self.captured()
        signature = webhook_signature(body)
        spaced = body.replace(b",", b", ")
        self.assertEqual(post_webhook(self.client, spaced, signature=signature).status_code, 400)

    def test_non_ascii_and_non_utf8_signature_or_body_give_400(self):
        self.assertEqual(
            post_webhook(self.client, self.captured(), signature="é" * 64).status_code, 400
        )
        self.assertEqual(
            post_webhook(self.client, b"\xff\xfe\x00", signature="a" * 64).status_code, 400
        )

    @override_settings(RAZORPAY_WEBHOOK_SECRET="")
    def test_not_configured_gives_503(self):
        self.assertEqual(post_webhook(self.client, self.captured(), signature="x").status_code, 503)

    def test_only_post_is_allowed(self):
        self.assertEqual(self.client.get("/api/payments/webhook/").status_code, 405)

    def test_works_without_csrf_cookie(self):
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(post_webhook(strict, self.captured()).status_code, 200)

    def test_valid_signature_but_body_not_json_is_400(self):
        body = b"this is not json"
        self.assertEqual(
            post_webhook(self.client, body, signature=webhook_signature(body)).status_code, 400
        )
        body = b'["a", "list"]'
        self.assertEqual(
            post_webhook(self.client, body, signature=webhook_signature(body)).status_code, 400
        )

    # ---- idempotency: the heart of Day 11 ----
    def test_same_event_delivered_twice_is_handled_once(self):
        body = self.captured()
        first = post_webhook(self.client, body, event_id="evt_1")
        second = post_webhook(self.client, body, event_id="evt_1")
        self.assertEqual(first.json(), {"status": "confirmed"})
        self.assertEqual(second.json(), {"status": "duplicate_ignored"})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(WebhookEvent.objects.count(), 1)

    def test_duplicate_does_not_touch_the_data_again(self):
        body = self.captured()
        post_webhook(self.client, body, event_id="evt_1")
        stamp = Payment.objects.get(pk=self.payment.pk).updated_at
        post_webhook(self.client, body, event_id="evt_1")
        self.assertEqual(Payment.objects.get(pk=self.payment.pk).updated_at, stamp)

    def test_missing_event_id_header_falls_back_to_a_body_hash(self):
        body = self.captured()
        first = post_webhook(self.client, body, event_id=None)
        second = post_webhook(self.client, body, event_id=None)
        self.assertEqual(first.json(), {"status": "confirmed"})
        self.assertEqual(second.json(), {"status": "duplicate_ignored"})

    def test_two_different_events_for_the_same_payment_settle_only_once(self):
        """Razorpay sends payment.captured AND order.paid for one payment."""
        first = post_webhook(self.client, self.captured(event="payment.captured"), event_id="evt_1")
        second = post_webhook(self.client, self.captured(event="order.paid"), event_id="evt_2")
        self.assertEqual(first.json(), {"status": "confirmed"})
        self.assertEqual(second.json(), {"status": "already_processed"})
        self.assertEqual(WebhookEvent.objects.count(), 2)
        self.assertEqual(Payment.objects.filter(status=PaymentStatus.PAID).count(), 1)

    def test_webhook_after_the_verify_endpoint_is_a_harmless_no_op(self):
        services.settle_successful_payment("order_A", "pay_A")  # what the verify endpoint does
        response = post_webhook(self.client, self.captured())
        self.assertEqual(response.json(), {"status": "already_processed"})

    # ---- paid after the hold expired, through the webhook ----
    def test_late_payment_after_expiry_with_free_slot_confirms(self):
        self.booking.status = BookingStatus.EXPIRED
        self.booking.save()
        response = post_webhook(self.client, self.captured())
        self.assertEqual(response.json(), {"status": "confirmed_after_hold_expired"})

    def test_late_payment_after_expiry_with_taken_slot_starts_a_refund(self):
        self.booking.status = BookingStatus.EXPIRED
        self.booking.save()
        self.make_booking(user=self.bob, minutes_left=5)
        with self.captureOnCommitCallbacks(execute=True):
            response = post_webhook(self.client, self.captured())
        self.assertEqual(response.json(), {"status": "refund_pending"})
        self.reload()
        self.assertEqual(self.payment.status, PaymentStatus.REFUND_PENDING)
        self.queue.assert_called_once_with(self.payment.pk)

    # ---- things that must NOT cause endless Razorpay retries ----
    def test_unknown_order_is_answered_200_and_ignored(self):
        response = post_webhook(self.client, webhook_body("order_STRANGER", "pay_X", 100))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored_unknown_order"})

    def test_unknown_event_type_is_answered_200_and_ignored(self):
        body = webhook_body("order_A", "pay_A", 50000, event="refund.processed")
        self.assertEqual(post_webhook(self.client, body).json(), {"status": "ignored_event"})
        self.reload()
        self.assertEqual(self.booking.status, BookingStatus.PENDING)

    def test_malformed_success_event_is_answered_200_and_ignored(self):
        body = b'{"event":"payment.captured","payload":{}}'
        response = post_webhook(self.client, body, signature=webhook_signature(body))
        self.assertEqual(response.json(), {"status": "ignored_malformed"})

    def test_amount_mismatch_confirms_nothing(self):
        response = post_webhook(self.client, self.captured(amount=100))
        self.assertEqual(response.json(), {"status": "amount_mismatch"})
        self.reload()
        self.assertEqual(self.booking.status, BookingStatus.PENDING)

    # ---- failed payments ----
    def test_payment_failed_event_notes_the_failure_but_keeps_the_booking(self):
        body = webhook_body("order_A", "pay_FAIL", 50000, event="payment.failed")
        response = post_webhook(self.client, body)
        self.assertEqual(response.json(), {"status": "payment_failed_recorded"})
        self.reload()
        self.assertEqual(self.payment.status, PaymentStatus.FAILED)
        self.assertEqual(self.payment.status_note, "Bank declined")
        self.assertEqual(self.booking.status, BookingStatus.PENDING)  # the user may try again
        self.assertIsNone(self.payment.razorpay_payment_id)

    def test_failed_attempt_then_successful_attempt_on_the_same_order(self):
        post_webhook(
            self.client,
            webhook_body("order_A", "pay_FAIL", 50000, "payment.failed"),
            event_id="evt_f",
        )
        response = post_webhook(self.client, self.captured(payment="pay_GOOD"), event_id="evt_ok")
        self.assertEqual(response.json(), {"status": "confirmed"})
        self.reload()
        self.assertEqual(self.payment.razorpay_payment_id, "pay_GOOD")

    def test_failure_event_after_payment_is_ignored(self):
        post_webhook(self.client, self.captured(), event_id="evt_1")
        body = webhook_body("order_A", "pay_FAIL", 50000, "payment.failed")
        self.assertEqual(
            post_webhook(self.client, body, event_id="evt_2").json(), {"status": "ignored"}
        )
        self.reload()
        self.assertEqual(self.payment.status, PaymentStatus.PAID)

    # ---- OUR crash must lead to a retry, not a lost event ----
    def test_crash_inside_processing_gives_500_and_the_retry_works(self):
        crashing = Client(raise_request_exception=False)
        with mock.patch(
            "payments.webhooks.settle_successful_payment", side_effect=RuntimeError("boom")
        ):
            response = post_webhook(crashing, self.captured(), event_id="evt_crash")
        self.assertEqual(response.status_code, 500)
        # The event must NOT be remembered, otherwise Razorpay's retry would be ignored forever.
        self.assertEqual(WebhookEvent.objects.count(), 0)
        self.reload()
        self.assertEqual(self.booking.status, BookingStatus.PENDING)

        retry = post_webhook(self.client, self.captured(), event_id="evt_crash")  # same event id
        self.assertEqual(retry.json(), {"status": "confirmed"})
