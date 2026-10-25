"""DAY 11: refunds (the task that gives the money back)."""

from decimal import Decimal
from unittest import mock

from django.test import TestCase

from bookings.models import BookingStatus
from payments import services, tasks
from payments.models import PaymentStatus

from .helpers import PaymentFixtureMixin, with_razorpay


@with_razorpay
class RefundTests(PaymentFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.fake = self.fake_razorpay()
        booking = self.make_booking(status=BookingStatus.CANCELLED)
        self.payment = self.make_payment(
            booking,
            "order_R",
            status=PaymentStatus.REFUND_PENDING,
            razorpay_payment_id="pay_R",
            status_note="test",
        )

    def reload(self):
        self.payment.refresh_from_db()

    def test_refund_calls_razorpay_with_the_right_payment_and_amount(self):
        self.assertEqual(services.refund_payment(self.payment.pk), "refunded")
        method, path, body = self.fake.calls[0]
        self.assertEqual((method, path), ("POST", "/v1/payments/pay_R/refund"))
        self.assertEqual(body["amount"], 50000)  # paise
        self.assertEqual(body["notes"], {"payment_pk": str(self.payment.pk)})
        self.reload()
        self.assertEqual(self.payment.status, PaymentStatus.REFUNDED)
        self.assertTrue(self.payment.razorpay_refund_id.startswith("rfnd_"))

    def test_refunding_twice_sends_only_one_refund(self):
        self.assertEqual(services.refund_payment(self.payment.pk), "refunded")
        self.assertEqual(services.refund_payment(self.payment.pk), "skipped")
        self.assertEqual(len(self.fake.calls_to("/v1/payments/")), 1)

    def test_only_refund_pending_payments_are_refunded(self):
        for status in (
            PaymentStatus.CREATED,
            PaymentStatus.PAID,
            PaymentStatus.FAILED,
            PaymentStatus.REFUNDED,
        ):
            self.payment.status = status
            self.payment.save()
            self.assertEqual(services.refund_payment(self.payment.pk), "skipped", status)
        self.assertEqual(self.fake.calls, [])

    def test_razorpay_failure_keeps_the_payment_pending_and_raises_for_retry(self):
        self.fake.error_status = 400
        with self.assertRaises(services.RefundFailed):
            services.refund_payment(self.payment.pk)
        self.reload()
        self.assertEqual(self.payment.status, PaymentStatus.REFUND_PENDING)
        self.assertIsNone(self.payment.razorpay_refund_id)

    def test_after_a_failed_try_the_next_try_can_succeed(self):
        self.fake.error_status = 500
        with self.assertRaises(services.RefundFailed):
            services.refund_payment(self.payment.pk)
        self.fake.error_status = None
        self.assertEqual(services.refund_payment(self.payment.pk), "refunded")

    def test_payment_without_a_razorpay_payment_id_cannot_be_refunded(self):
        self.payment.razorpay_payment_id = None
        self.payment.save()
        self.assertEqual(services.refund_payment(self.payment.pk), "no_payment_id")
        self.assertEqual(self.fake.calls, [])

    def test_missing_payment_is_reported_not_crashed(self):
        self.assertEqual(services.refund_payment(999999), "missing")

    def test_odd_amount_is_refunded_exactly(self):
        self.payment.amount = Decimal("19.99")
        self.payment.save()
        services.refund_payment(self.payment.pk)
        self.assertEqual(self.fake.calls[0][2]["amount"], 1999)

    # ---- the Celery tasks around it ----
    def test_process_refund_task_runs_the_refund(self):
        self.assertEqual(tasks.process_refund.run(self.payment.pk), "refunded")

    def test_task_is_configured_to_retry_a_failed_refund(self):
        self.assertIn(services.RefundFailed, tasks.process_refund.autoretry_for)
        self.assertEqual(tasks.process_refund.max_retries, 5)

    def test_sweeper_queues_every_pending_refund_and_nothing_else(self):
        booking = self.make_booking(
            slot=self.make_slot(hours_from_now=70), status=BookingStatus.CANCELLED
        )
        second = self.make_payment(
            booking, "order_R2", status=PaymentStatus.REFUND_PENDING, razorpay_payment_id="pay_R2"
        )
        paid = self.make_payment(
            self.make_booking(slot=self.make_slot(hours_from_now=71)),
            "order_P",
            status=PaymentStatus.PAID,
        )
        with mock.patch("payments.tasks.process_refund.delay") as queued:
            count = tasks.process_pending_refunds()
        self.assertEqual(count, 2)
        queued_ids = sorted(call.args[0] for call in queued.call_args_list)
        self.assertEqual(queued_ids, sorted([self.payment.pk, second.pk]))
        self.assertNotIn(paid.pk, queued_ids)

    def test_sweeper_with_nothing_to_do_returns_zero(self):
        self.payment.status = PaymentStatus.REFUNDED
        self.payment.save()
        with mock.patch("payments.tasks.process_refund.delay") as queued:
            self.assertEqual(tasks.process_pending_refunds(), 0)
        queued.assert_not_called()

    def test_a_dead_queue_does_not_crash_the_webhook_path(self):
        """If Redis is down when we try to queue the refund, the payment still becomes
        REFUND_PENDING (the sweeper will pick it up later) and nothing crashes."""
        booking = self.make_booking(
            slot=self.make_slot(hours_from_now=80), status=BookingStatus.CANCELLED
        )
        self.make_payment(booking, "order_Q")
        with mock.patch(
            "payments.tasks.process_refund.delay", side_effect=ConnectionError("redis down")
        ):
            with self.captureOnCommitCallbacks(execute=True):
                result = services.settle_successful_payment("order_Q", "pay_Q")
        self.assertEqual(result.outcome, services.OUTCOME_REFUND)
        result.payment.refresh_from_db()
        self.assertEqual(result.payment.status, PaymentStatus.REFUND_PENDING)
