"""DAY 2 tests: PROVE that PostgreSQL itself blocks bad data."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase
from django.utils import timezone

from bookings.models import Booking, BookingStatus
from payments.models import Payment
from venues.models import Slot, Venue

User = get_user_model()


class ModelRulesTest(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="pass12345")
        self.user_a = User.objects.create_user("alice", password="pass12345")
        self.user_b = User.objects.create_user("bob", password="pass12345")
        self.venue = Venue.objects.create(
            owner=self.owner, name="City Turf", address="Sector 5", city="Ghaziabad"
        )
        start = timezone.now() + timedelta(days=1)
        self.slot = Slot.objects.create(
            venue=self.venue,
            start_time=start,
            end_time=start + timedelta(hours=1),
            price=Decimal("500.00"),
        )

    def make_booking(self, user, status=BookingStatus.PENDING):
        return Booking.objects.create(
            user=user,
            slot=self.slot,
            status=status,
            expires_at=timezone.now() + timedelta(minutes=10),
        )

    # ---- the key rule: one active booking per slot ----
    def test_second_pending_booking_is_blocked(self):
        self.make_booking(self.user_a)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():  # keeps the test transaction usable after the error
                self.make_booking(self.user_b)

    def test_pending_blocks_confirmed(self):
        self.make_booking(self.user_a, BookingStatus.PENDING)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make_booking(self.user_b, BookingStatus.CONFIRMED)

    def test_cancelled_booking_does_not_block_new_one(self):
        self.make_booking(self.user_a, BookingStatus.CANCELLED)
        self.make_booking(self.user_b)  # must work
        self.assertEqual(Booking.objects.filter(slot=self.slot).count(), 2)

    def test_expired_booking_does_not_block_new_one(self):
        self.make_booking(self.user_a, BookingStatus.EXPIRED)
        self.make_booking(self.user_b)  # must work

    def test_many_cancelled_bookings_are_allowed(self):
        self.make_booking(self.user_a, BookingStatus.CANCELLED)
        self.make_booking(self.user_b, BookingStatus.CANCELLED)
        self.assertEqual(Booking.objects.filter(slot=self.slot).count(), 2)

    def test_pending_booking_must_have_expiry(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Booking.objects.create(user=self.user_a, slot=self.slot, expires_at=None)

    # ---- slot rules ----
    def test_slot_end_before_start_is_rejected(self):
        start = timezone.now() + timedelta(days=2)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Slot.objects.create(
                    venue=self.venue, start_time=start,
                    end_time=start - timedelta(hours=1), price=Decimal("100"),
                )

    def test_negative_price_is_rejected(self):
        start = timezone.now() + timedelta(days=3)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Slot.objects.create(
                    venue=self.venue, start_time=start,
                    end_time=start + timedelta(hours=1), price=Decimal("-1"),
                )

    def test_same_start_time_twice_at_one_venue_is_rejected(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Slot.objects.create(
                    venue=self.venue, start_time=self.slot.start_time,
                    end_time=self.slot.end_time, price=Decimal("100"),
                )

    # ---- payment rules ----
    def test_payment_order_id_must_be_unique(self):
        booking = self.make_booking(self.user_a)
        Payment.objects.create(booking=booking, razorpay_order_id="order_1", amount=Decimal("500"))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Payment.objects.create(
                    booking=booking, razorpay_order_id="order_1", amount=Decimal("500")
                )

    def test_many_unpaid_payments_with_empty_payment_id_are_allowed(self):
        booking = self.make_booking(self.user_a)
        Payment.objects.create(booking=booking, razorpay_order_id="order_1", amount=Decimal("500"))
        Payment.objects.create(booking=booking, razorpay_order_id="order_2", amount=Decimal("500"))
        self.assertEqual(booking.payments.count(), 2)

    def test_payment_amount_must_be_positive(self):
        booking = self.make_booking(self.user_a)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Payment.objects.create(booking=booking, razorpay_order_id="o", amount=Decimal("0"))

    # ---- protect rules ----
    def test_cannot_delete_slot_that_has_bookings(self):
        self.make_booking(self.user_a)
        with self.assertRaises(ProtectedError):
            self.slot.delete()
