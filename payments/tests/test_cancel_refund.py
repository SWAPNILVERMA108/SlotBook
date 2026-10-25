"""DAY 11: cancelling a booking the user already PAID for must start a refund."""

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from bookings import services as booking_services
from bookings.models import BookingStatus
from payments import services
from payments.models import PaymentStatus

from .helpers import PaymentFixtureMixin, with_razorpay


@with_razorpay
class CancelPaidBookingTests(PaymentFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.queue = self.capture_refund_queue()
        self.fake = self.fake_razorpay()
        self.booking = self.make_booking(status=BookingStatus.CONFIRMED)
        self.payment = self.make_payment(
            self.booking, "order_A", status=PaymentStatus.PAID, razorpay_payment_id="pay_A"
        )

    def cancel(self):
        with self.captureOnCommitCallbacks(execute=True):
            return booking_services.cancel_booking(self.alice, self.booking.id)

    def test_cancelling_a_paid_booking_starts_the_refund(self):
        self.cancel()
        self.booking.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CANCELLED)
        self.assertEqual(self.payment.status, PaymentStatus.REFUND_PENDING)
        self.assertIn("cancelled by the user", self.payment.status_note)
        self.queue.assert_called_once_with(self.payment.pk)

    def test_the_refund_really_reaches_razorpay_and_marks_the_payment_refunded(self):
        self.cancel()
        self.assertEqual(services.refund_payment(self.payment.pk), "refunded")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.REFUNDED)
        self.assertEqual(self.fake.calls[0][1], "/v1/payments/pay_A/refund")
        self.assertEqual(self.fake.calls[0][2]["amount"], 50000)

    def test_cancelling_a_pending_booking_starts_no_refund(self):
        pending = self.make_booking(slot=self.make_slot(hours_from_now=40))
        unpaid = self.make_payment(pending, "order_P")  # order opened but never paid
        with self.captureOnCommitCallbacks(execute=True):
            booking_services.cancel_booking(self.alice, pending.id)
        unpaid.refresh_from_db()
        self.assertEqual(unpaid.status, PaymentStatus.CREATED)
        self.queue.assert_not_called()

    def test_paying_an_order_of_a_cancelled_pending_booking_is_refunded(self):
        pending = self.make_booking(slot=self.make_slot(hours_from_now=41))
        unpaid = self.make_payment(pending, "order_P")
        booking_services.cancel_booking(self.alice, pending.id)
        with self.captureOnCommitCallbacks(execute=True):
            result = services.settle_successful_payment("order_P", "pay_P")
        self.assertEqual(result.outcome, services.OUTCOME_REFUND)
        unpaid.refresh_from_db()
        self.assertEqual(unpaid.status, PaymentStatus.REFUND_PENDING)

    def test_cancelling_a_free_confirmed_booking_has_nothing_to_refund(self):
        free = self.make_booking(
            slot=self.make_slot(hours_from_now=42), status=BookingStatus.CONFIRMED
        )
        self.cancel_other(free)
        self.queue.assert_not_called()

    def cancel_other(self, booking):
        with self.captureOnCommitCallbacks(execute=True):
            return booking_services.cancel_booking(self.alice, booking.id)

    def test_only_the_paid_payment_is_refunded_not_failed_or_open_ones(self):
        failed = self.make_payment(self.booking, "order_F", status=PaymentStatus.FAILED)
        open_order = self.make_payment(self.booking, "order_O")
        self.cancel()
        for payment, expected in (
            (failed, PaymentStatus.FAILED),
            (open_order, PaymentStatus.CREATED),
            (self.payment, PaymentStatus.REFUND_PENDING),
        ):
            payment.refresh_from_db()
            self.assertEqual(payment.status, expected)
        self.queue.assert_called_once_with(self.payment.pk)

    def test_cancelling_twice_refunds_only_once(self):
        self.cancel()
        with self.assertRaises(booking_services.BookingNotCancellable):
            self.cancel()
        self.assertEqual(self.queue.call_count, 1)

    def test_a_refund_that_is_already_underway_is_not_started_again(self):
        self.payment.status = PaymentStatus.REFUND_PENDING
        self.payment.save()
        self.cancel()
        self.queue.assert_not_called()

    def test_through_the_api_the_user_gets_cancelled_and_the_refund_is_queued(self):
        api = APIClient()
        api.force_authenticate(self.alice)
        with self.captureOnCommitCallbacks(execute=True):
            response = api.post(reverse("booking-cancel", args=[self.booking.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "CANCELLED")
        self.queue.assert_called_once_with(self.payment.pk)
