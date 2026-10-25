"""DAY 10 + 11: the verify endpoint and the decision table of settle_successful_payment()."""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import Booking, BookingStatus
from payments import services
from payments.models import Payment, PaymentStatus

from .helpers import PaymentFixtureMixin, checkout_signature, with_razorpay


@with_razorpay
class VerifyEndpointTests(PaymentFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()
        self.queue = self.capture_refund_queue()
        self.booking = self.make_booking()
        self.payment = self.make_payment(self.booking, order_id="order_A")
        self.api = APIClient()
        self.api.force_authenticate(self.alice)
        self.url = reverse("payment-verify")

    def verify(self, order="order_A", payment="pay_A", signature=None, client=None):
        body = {
            "razorpay_order_id": order,
            "razorpay_payment_id": payment,
            "razorpay_signature": signature
            if signature is not None
            else checkout_signature(order, payment),
        }
        return (client or self.api).post(self.url, body, format="json")

    def test_requires_login(self):
        self.assertEqual(self.verify(client=APIClient()).status_code, 401)

    def test_valid_signature_confirms_the_booking(self):
        response = self.verify()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["outcome"], "confirmed")
        self.assertEqual(response.json()["booking_status"], "CONFIRMED")
        self.booking.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.CONFIRMED)
        self.assertEqual(self.payment.status, PaymentStatus.PAID)
        self.assertEqual(self.payment.razorpay_payment_id, "pay_A")

    def test_invalid_signature_changes_nothing(self):
        response = self.verify(signature="0" * 64)
        self.assertEqual(response.status_code, 400)
        self.booking.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.PENDING)
        self.assertEqual(self.payment.status, PaymentStatus.CREATED)

    def test_signature_made_for_another_payment_is_rejected(self):
        self.assertEqual(
            self.verify(signature=checkout_signature("order_A", "pay_OTHER")).status_code, 400
        )

    def test_signature_made_with_the_wrong_secret_is_rejected(self):
        forged = checkout_signature("order_A", "pay_A", secret="attacker_guess")
        self.assertEqual(self.verify(signature=forged).status_code, 400)

    def test_garbage_and_non_ascii_signatures_give_400_not_500(self):
        for signature in ("not-a-signature", "é" * 64, " ", "x" * 128):
            self.assertEqual(self.verify(signature=signature).status_code, 400, repr(signature))

    def test_overlong_values_are_rejected_by_the_serializer(self):
        self.assertEqual(self.verify(order="o" * 200).status_code, 400)

    def test_missing_fields_give_400(self):
        self.assertEqual(
            self.api.post(self.url, {"razorpay_order_id": "order_A"}, format="json").status_code,
            400,
        )

    def test_someone_elses_order_is_404_even_with_a_valid_signature(self):
        other = APIClient()
        other.force_authenticate(self.bob)
        self.assertEqual(self.verify(client=other).status_code, 404)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, BookingStatus.PENDING)

    def test_unknown_order_is_404(self):
        self.assertEqual(self.verify(order="order_NOPE", payment="pay_X").status_code, 404)

    def test_verifying_twice_is_harmless(self):
        self.verify()
        again = self.verify()
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["outcome"], "already_processed")
        self.assertEqual(Booking.objects.filter(status=BookingStatus.CONFIRMED).count(), 1)
        self.assertEqual(Payment.objects.count(), 1)


@with_razorpay
class SettlePaymentDecisionTableTests(PaymentFixtureMixin, TestCase):
    """Every row of the decision table in settle_successful_payment()."""

    def setUp(self):
        self.make_world()
        self.queue = self.capture_refund_queue()

    def settle(self, order="order_A", payment="pay_A", amount=None):
        with self.captureOnCommitCallbacks(execute=True):
            return services.settle_successful_payment(order, payment, amount_paise=amount)

    def reload(self, *objs):
        for obj in objs:
            obj.refresh_from_db()

    # ---- the happy paths ----
    def test_pending_booking_is_confirmed(self):
        booking = self.make_booking()
        payment = self.make_payment(booking, "order_A")
        result = self.settle(amount=50000)
        self.assertEqual(result.outcome, services.OUTCOME_CONFIRMED)
        self.reload(booking, payment)
        self.assertEqual(
            (booking.status, payment.status), (BookingStatus.CONFIRMED, PaymentStatus.PAID)
        )
        self.queue.assert_not_called()

    def test_hold_ended_but_celery_has_not_run_yet_is_still_confirmed(self):
        """Still PENDING in the database = nobody else could have taken the slot. Honour the payment."""
        booking = self.make_booking(minutes_left=-3)
        self.make_payment(booking, "order_A")
        self.assertEqual(self.settle().outcome, services.OUTCOME_CONFIRMED)

    # ---- paid AFTER the hold expired (the famous edge case) ----
    def test_expired_but_slot_still_free_is_confirmed_again(self):
        booking = self.make_booking(status=BookingStatus.EXPIRED)
        payment = self.make_payment(booking, "order_A")
        result = self.settle()
        self.assertEqual(result.outcome, services.OUTCOME_RECONFIRMED)
        self.reload(booking, payment)
        self.assertEqual(booking.status, BookingStatus.CONFIRMED)
        self.assertEqual(payment.status, PaymentStatus.PAID)
        self.assertIn("after the hold expired", payment.status_note)
        self.queue.assert_not_called()

    def test_expired_and_someone_else_has_a_live_hold_means_refund(self):
        booking = self.make_booking(status=BookingStatus.EXPIRED)
        payment = self.make_payment(booking, "order_A")
        rival = self.make_booking(user=self.bob, minutes_left=5)  # Bob took the slot meanwhile
        result = self.settle()
        self.assertEqual(result.outcome, services.OUTCOME_REFUND)
        self.reload(booking, payment, rival)
        self.assertEqual(booking.status, BookingStatus.EXPIRED)  # NOT resurrected
        self.assertEqual(rival.status, BookingStatus.PENDING)  # Bob keeps his hold
        self.assertEqual(payment.status, PaymentStatus.REFUND_PENDING)
        self.assertIn("someone else", payment.status_note)
        self.queue.assert_called_once_with(payment.pk)  # refund was queued

    def test_expired_and_someone_else_confirmed_means_refund(self):
        booking = self.make_booking(status=BookingStatus.EXPIRED)
        payment = self.make_payment(booking, "order_A")
        self.make_booking(user=self.bob, status=BookingStatus.CONFIRMED)
        self.assertEqual(self.settle().outcome, services.OUTCOME_REFUND)
        self.reload(payment)
        self.assertEqual(payment.status, PaymentStatus.REFUND_PENDING)

    def test_other_users_stale_pending_hold_does_not_block_reconfirmation(self):
        """Bob's hold also ended (Celery has not cleaned it). It must not stop Alice's late payment."""
        booking = self.make_booking(status=BookingStatus.EXPIRED)
        payment = self.make_payment(booking, "order_A")
        stale = self.make_booking(user=self.bob, minutes_left=-2)
        self.assertEqual(self.settle().outcome, services.OUTCOME_RECONFIRMED)
        self.reload(booking, payment, stale)
        self.assertEqual(booking.status, BookingStatus.CONFIRMED)
        self.assertEqual(stale.status, BookingStatus.EXPIRED)  # cleaned up, so the DB rule holds
        self.assertEqual(
            Booking.objects.filter(slot=self.slot, status=BookingStatus.CONFIRMED).count(), 1
        )

    def test_expired_and_slot_already_started_means_refund(self):
        slot = self.make_slot(hours_from_now=1)
        booking = self.make_booking(slot=slot, status=BookingStatus.EXPIRED)
        self.make_payment(booking, "order_A")
        type(slot).objects.filter(pk=slot.pk).update(
            start_time=timezone.now() - timedelta(minutes=1)
        )
        self.assertEqual(self.settle().outcome, services.OUTCOME_REFUND)

    def test_expired_and_slot_switched_off_means_refund(self):
        booking = self.make_booking(status=BookingStatus.EXPIRED)
        self.make_payment(booking, "order_A")
        type(self.slot).objects.filter(pk=self.slot.pk).update(is_active=False)
        self.assertEqual(self.settle().outcome, services.OUTCOME_REFUND)

    # ---- other states ----
    def test_cancelled_booking_means_refund_and_stays_cancelled(self):
        booking = self.make_booking(status=BookingStatus.CANCELLED)
        payment = self.make_payment(booking, "order_A")
        self.assertEqual(self.settle().outcome, services.OUTCOME_REFUND)
        self.reload(booking, payment)
        self.assertEqual(booking.status, BookingStatus.CANCELLED)
        self.assertIn("cancelled", payment.status_note)

    def test_refund_path_stores_the_razorpay_payment_id_so_the_refund_can_be_sent(self):
        booking = self.make_booking(status=BookingStatus.CANCELLED)
        payment = self.make_payment(booking, "order_A")
        self.settle(payment="pay_TO_REFUND")
        self.reload(payment)
        self.assertEqual(payment.razorpay_payment_id, "pay_TO_REFUND")

    def test_second_payment_for_an_already_confirmed_booking_means_refund(self):
        booking = self.make_booking(status=BookingStatus.CONFIRMED)
        payment = self.make_payment(booking, "order_B")
        self.assertEqual(
            self.settle(order="order_B", payment="pay_B").outcome, services.OUTCOME_REFUND
        )
        self.reload(booking, payment)
        self.assertEqual(booking.status, BookingStatus.CONFIRMED)
        self.assertIn("duplicate", payment.status_note)

    # ---- idempotency and safety ----
    def test_settling_twice_changes_nothing_the_second_time(self):
        booking = self.make_booking()
        payment = self.make_payment(booking, "order_A")
        self.settle()
        stamp = Payment.objects.get(pk=payment.pk).updated_at
        again = self.settle()
        self.assertEqual(again.outcome, services.OUTCOME_DUPLICATE)
        self.assertEqual(Payment.objects.get(pk=payment.pk).updated_at, stamp)

    def test_refund_pending_payment_is_not_queued_twice(self):
        booking = self.make_booking(status=BookingStatus.CANCELLED)
        self.make_payment(booking, "order_A")
        self.settle()
        self.settle()
        self.assertEqual(self.queue.call_count, 1)

    def test_amount_mismatch_confirms_nothing(self):
        booking = self.make_booking()
        payment = self.make_payment(booking, "order_A")
        result = self.settle(amount=100)  # paid 1 rupee instead of 500
        self.assertEqual(result.outcome, services.OUTCOME_AMOUNT_MISMATCH)
        self.reload(booking, payment)
        self.assertEqual(booking.status, BookingStatus.PENDING)
        self.assertEqual(payment.status, PaymentStatus.CREATED)
        self.assertIn("MISMATCH", payment.status_note)

    def test_failed_attempt_followed_by_a_good_one_still_settles(self):
        booking = self.make_booking()
        payment = self.make_payment(booking, "order_A", status=PaymentStatus.FAILED)
        self.assertEqual(self.settle().outcome, services.OUTCOME_CONFIRMED)
        self.reload(payment)
        self.assertEqual(payment.status, PaymentStatus.PAID)

    def test_unknown_order_raises_payment_not_found(self):
        with self.assertRaises(services.PaymentNotFound):
            services.settle_successful_payment("order_NOPE", "pay_X")

    def test_amount_is_compared_in_paise_for_odd_prices(self):
        slot = self.make_slot(price=Decimal("19.99"), hours_from_now=60)
        booking = self.make_booking(slot=slot)
        self.make_payment(booking, "order_C", amount=Decimal("19.99"))
        self.assertEqual(
            self.settle(order="order_C", payment="pay_C", amount=1999).outcome,
            services.OUTCOME_CONFIRMED,
        )

    def test_refund_is_queued_only_after_commit(self):
        booking = self.make_booking(status=BookingStatus.CANCELLED)
        self.make_payment(booking, "order_A")
        services.settle_successful_payment("order_A", "pay_A")  # no on_commit execution here
        self.queue.assert_not_called()
