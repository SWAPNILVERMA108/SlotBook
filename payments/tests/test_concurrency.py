"""DAY 11: payments under real concurrency (threads + real committed transactions).

Run:  python manage.py test payments.tests.test_concurrency -v 2
"""

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from django.db import connection
from django.test import Client, TransactionTestCase

from bookings import services as booking_services
from bookings import tasks as booking_tasks
from bookings.models import ACTIVE_STATUSES, Booking, BookingStatus
from payments import services
from payments.models import Payment, PaymentStatus, WebhookEvent

from .helpers import (
    PaymentFixtureMixin,
    checkout_signature,
    post_webhook,
    webhook_body,
    with_razorpay,
)


def run_together(jobs):
    """Runs every job in its own thread, all released at the same moment. Returns their results."""
    barrier = threading.Barrier(len(jobs))
    results = []
    guard = threading.Lock()

    def worker(job):
        barrier.wait()
        try:
            outcome = job()
        except Exception as error:  # noqa: BLE001  (we want to SEE every kind of failure)
            outcome = error
        finally:
            connection.close()
        with guard:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        list(pool.map(worker, jobs))
    return results


def webhook_job(body, event_id):
    def job():
        return post_webhook(Client(), body, event_id=event_id).json()["status"]

    return job


@with_razorpay
class PaymentRaceTests(PaymentFixtureMixin, TransactionTestCase):
    def setUp(self):
        self.make_world()
        self.queue = self.capture_refund_queue()
        self.extra_users = [self.make_user(f"extra{i}") for i in range(15)]

    def make_user(self, name):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(name)

    def active_count(self, slot):
        return Booking.objects.filter(slot=slot, status__in=ACTIVE_STATUSES).count()

    # ---- 1. Razorpay delivers the same event 20 times at once ----
    def test_20_simultaneous_copies_of_one_event_are_handled_once(self):
        booking = self.make_booking()
        payment = self.make_payment(booking, "order_A")
        body = webhook_body("order_A", "pay_A", 50000)

        results = run_together([webhook_job(body, "evt_same")] * 20)

        print(f"\n[payments 1] 20 copies of one event: {dict(Counter(map(str, results)))}")
        self.assertEqual(Counter(results), Counter({"confirmed": 1, "duplicate_ignored": 19}))
        self.assertEqual(WebhookEvent.objects.count(), 1)
        booking.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(
            (booking.status, payment.status), (BookingStatus.CONFIRMED, PaymentStatus.PAID)
        )

    # ---- 2. the browser callback and the webhook arrive together ----
    def test_verify_callback_and_webhook_at_the_same_moment(self):
        booking = self.make_booking()
        payment = self.make_payment(booking, "order_A")
        body = webhook_body("order_A", "pay_A", 50000)

        def verify_job():
            return services.settle_successful_payment("order_A", "pay_A").outcome

        results = run_together(
            [verify_job, webhook_job(body, "evt_1"), verify_job, webhook_job(body, "evt_2")]
        )

        print(f"\n[payments 2] verify + webhook together: {sorted(map(str, results))}")
        self.assertFalse([r for r in results if isinstance(r, Exception)])
        self.assertEqual(sum(1 for r in results if r == "confirmed"), 1)  # exactly one did the work
        booking.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(
            (booking.status, payment.status), (BookingStatus.CONFIRMED, PaymentStatus.PAID)
        )

    # ---- 3. THE edge case: a late payment races with a new customer ----
    def test_late_payment_versus_a_new_customer_never_double_books(self):
        outcomes = Counter()
        for round_number in range(8):
            slot = self.make_slot(hours_from_now=30 + round_number)
            late = self.make_booking(user=self.alice, slot=slot, status=BookingStatus.EXPIRED)
            payment = self.make_payment(late, f"order_L{round_number}")
            body = webhook_body(payment.razorpay_order_id, f"pay_L{round_number}", 50000)

            jobs = [webhook_job(body, f"evt_L{round_number}")]
            jobs += [
                lambda user=user, slot=slot: booking_services.create_booking(user, slot.id)
                for user in self.extra_users[:6]
            ]
            results = run_together(jobs)

            unexpected = [
                r
                for r in results
                if isinstance(r, Exception) and not isinstance(r, booking_services.SlotUnavailable)
            ]
            self.assertEqual(unexpected, [], f"round {round_number}")
            self.assertEqual(self.active_count(slot), 1, f"round {round_number}: double booking!")

            payment.refresh_from_db()
            late.refresh_from_db()
            if late.status == BookingStatus.CONFIRMED:  # the late payer won the slot
                self.assertEqual(payment.status, PaymentStatus.PAID)
                self.assertFalse([r for r in results if isinstance(r, Booking)])
                outcomes["late payer won"] += 1
            else:  # a new customer won, so the payer is refunded
                self.assertEqual(payment.status, PaymentStatus.REFUND_PENDING)
                self.assertEqual(len([r for r in results if isinstance(r, Booking)]), 1)
                outcomes["new customer won, payer refunded"] += 1
        print(f"\n[payments 3] 8 rounds of late payment vs new customers: {dict(outcomes)}")

    # ---- 4. the Celery expiry task runs at the very moment the payment arrives ----
    def test_expiry_task_versus_payment_always_ends_confirmed_and_paid(self):
        for round_number in range(8):
            slot = self.make_slot(hours_from_now=60 + round_number)
            booking = self.make_booking(slot=slot, minutes_left=-1)  # hold ended, still PENDING
            payment = self.make_payment(booking, f"order_E{round_number}")
            body = webhook_body(payment.razorpay_order_id, f"pay_E{round_number}", 50000)

            results = run_together(
                [booking_tasks.release_expired_bookings, webhook_job(body, f"evt_E{round_number}")]
            )

            self.assertFalse(
                [r for r in results if isinstance(r, Exception)], f"round {round_number}: {results}"
            )
            booking.refresh_from_db()
            payment.refresh_from_db()
            # Whichever came first, the payer keeps the slot: PENDING -> CONFIRMED, or
            # PENDING -> EXPIRED -> CONFIRMED AGAIN (slot was still free).
            self.assertEqual(booking.status, BookingStatus.CONFIRMED, f"round {round_number}")
            self.assertEqual(payment.status, PaymentStatus.PAID, f"round {round_number}")

    # ---- 5. the user cancels at the very moment the payment arrives ----
    def test_cancel_versus_payment_never_keeps_the_money_without_a_booking(self):
        """Two orders are possible, and BOTH must end the same way: booking CANCELLED and the
        payment on its way back (REFUND_PENDING). 'Booking cancelled but payment still PAID' was a
        real bug that this test found, and the fix in cancel_booking() removed it."""
        orders_seen = Counter()
        for round_number in range(10):
            slot = self.make_slot(hours_from_now=100 + round_number)
            booking = self.make_booking(slot=slot)
            payment = self.make_payment(booking, f"order_C{round_number}")
            body = webhook_body(payment.razorpay_order_id, f"pay_C{round_number}", 50000)

            results = run_together(
                [
                    lambda b=booking: booking_services.cancel_booking(self.alice, b.id),
                    webhook_job(body, f"evt_C{round_number}"),
                ]
            )

            self.assertFalse(
                [r for r in results if isinstance(r, Exception)], f"round {round_number}: {results}"
            )
            booking.refresh_from_db()
            payment.refresh_from_db()
            self.assertEqual(booking.status, BookingStatus.CANCELLED, f"round {round_number}")
            self.assertEqual(
                payment.status, PaymentStatus.REFUND_PENDING, f"round {round_number}: {results}"
            )
            webhook_result = next(r for r in results if isinstance(r, str))
            orders_seen[
                "payment settled first" if webhook_result == "confirmed" else "cancel first"
            ] += 1
        print(f"\n[payments 5] 10 rounds of cancel vs payment: {dict(orders_seen)}")

    # ---- 6. verify endpoint signature helper sanity (no threads) ----
    def test_checkout_signature_helper_matches_the_real_library(self):
        booking = self.make_booking()
        self.make_payment(booking, "order_S")
        services.verify_checkout_signature(
            "order_S", "pay_S", checkout_signature("order_S", "pay_S")
        )
        with self.assertRaises(services.InvalidSignature):
            services.verify_checkout_signature("order_S", "pay_S", "0" * 64)
        self.assertEqual(Payment.objects.count(), 1)
