"""DAY 6 tests: the SAFE create_booking(), checked rule by rule and under real concurrency."""
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from bookings import services
from bookings.models import Booking, BookingStatus
from venues.models import Slot, Venue

User = get_user_model()


class CreateBookingRulesTest(TestCase):
    """One thread, one rule at a time."""

    def setUp(self):
        owner = User.objects.create_user("owner")
        self.alice = User.objects.create_user("alice")
        self.bob = User.objects.create_user("bob")
        self.venue = Venue.objects.create(owner=owner, name="City Turf", address="A", city="Ghaziabad")
        start = timezone.now() + timedelta(days=1)
        self.slot = Slot.objects.create(
            venue=self.venue, start_time=start, end_time=start + timedelta(hours=1), price=Decimal("500"),
        )

    def hold(self, user, status, expires_in_minutes=10):
        return Booking.objects.create(
            user=user, slot=self.slot, status=status,
            expires_at=timezone.now() + timedelta(minutes=expires_in_minutes),
        )

    def test_free_slot_can_be_booked_and_gets_a_hold(self):
        booking = services.create_booking(self.alice, self.slot.id)
        self.assertEqual(booking.status, BookingStatus.PENDING)
        self.assertEqual(booking.user, self.alice)
        hold = booking.expires_at - timezone.now()
        self.assertTrue(timedelta(minutes=9) < hold <= timedelta(minutes=10))

    def test_live_pending_hold_blocks_the_slot(self):
        self.hold(self.alice, BookingStatus.PENDING)
        with self.assertRaises(services.SlotUnavailable):
            services.create_booking(self.bob, self.slot.id)

    def test_confirmed_booking_blocks_the_slot(self):
        self.hold(self.alice, BookingStatus.CONFIRMED)
        with self.assertRaises(services.SlotUnavailable):
            services.create_booking(self.bob, self.slot.id)

    def test_same_user_clicking_twice_gets_slot_unavailable(self):
        services.create_booking(self.alice, self.slot.id)
        with self.assertRaises(services.SlotUnavailable):
            services.create_booking(self.alice, self.slot.id)

    def test_cancelled_and_expired_bookings_do_not_block(self):
        self.hold(self.alice, BookingStatus.CANCELLED)
        self.hold(self.alice, BookingStatus.EXPIRED)
        booking = services.create_booking(self.bob, self.slot.id)
        self.assertEqual(booking.status, BookingStatus.PENDING)

    def test_stale_pending_hold_does_not_block_and_is_marked_expired(self):
        """The hold ended 5 minutes ago but Celery has not cleaned it up yet."""
        stale = self.hold(self.alice, BookingStatus.PENDING, expires_in_minutes=-5)
        booking = services.create_booking(self.bob, self.slot.id)  # must NOT hit the DB constraint
        stale.refresh_from_db()
        self.assertEqual(stale.status, BookingStatus.EXPIRED)
        self.assertGreater(stale.updated_at, stale.created_at)  # updated_at was refreshed too
        self.assertEqual(booking.user, self.bob)
        self.assertEqual(Booking.objects.filter(slot=self.slot, status=BookingStatus.PENDING).count(), 1)

    def test_a_refused_booking_changes_nothing(self):
        live = self.hold(self.alice, BookingStatus.PENDING)
        before = Booking.objects.count()
        with self.assertRaises(services.SlotUnavailable):
            services.create_booking(self.bob, self.slot.id)
        self.assertEqual(Booking.objects.count(), before)
        live.refresh_from_db()
        self.assertEqual(live.status, BookingStatus.PENDING)

    def test_unknown_inactive_and_past_slots_raise_slot_not_found(self):
        inactive = Slot.objects.create(
            venue=self.venue, start_time=timezone.now() + timedelta(days=2),
            end_time=timezone.now() + timedelta(days=2, hours=1), price=Decimal("1"), is_active=False,
        )
        past = Slot.objects.create(
            venue=self.venue, start_time=timezone.now() - timedelta(hours=2),
            end_time=timezone.now() - timedelta(hours=1), price=Decimal("1"),
        )
        for slot_id in (999999, inactive.id, past.id):
            with self.assertRaises(services.SlotNotFound):
                services.create_booking(self.alice, slot_id)

    def test_closed_venue_slot_cannot_be_booked(self):
        self.venue.is_active = False
        self.venue.save()
        with self.assertRaises(services.SlotNotFound):
            services.create_booking(self.alice, self.slot.id)

    def test_hold_length_follows_the_setting(self):
        with self.settings(BOOKING_HOLD_MINUTES=3):
            booking = services.create_booking(self.alice, self.slot.id)
        hold = booking.expires_at - timezone.now()
        self.assertTrue(timedelta(minutes=2) < hold <= timedelta(minutes=3))


class CreateBookingConcurrencyTest(TransactionTestCase):
    """Many threads at the same moment (real committed transactions, like production)."""

    THREADS = 50

    def setUp(self):
        owner = User.objects.create_user("owner")
        self.users = [User.objects.create_user(f"user{i}") for i in range(self.THREADS)]
        self.venue = Venue.objects.create(owner=owner, name="City Turf", address="A", city="Ghaziabad")
        self.base = timezone.now() + timedelta(days=1)

    def make_slot(self, hours):
        start = self.base + timedelta(hours=hours)
        return Slot.objects.create(
            venue=self.venue, start_time=start, end_time=start + timedelta(hours=1), price=Decimal("500"),
        )

    def hammer(self, jobs):
        """jobs = list of (user, slot_id). Runs them all at the same moment, returns a Counter."""
        barrier = threading.Barrier(len(jobs), timeout=60)
        outcomes, guard = [], threading.Lock()

        def worker(job):
            user, slot_id = job
            try:
                connection.ensure_connection()  # open the connection BEFORE the start signal
            except Exception:
                pass  # the real call below will fail loudly and be counted as UNEXPECTED
            barrier.wait()
            try:
                services.create_booking(user, slot_id)
                outcome = "success"
            except services.SlotUnavailable:
                outcome = "unavailable"
            except Exception as error:
                outcome = f"UNEXPECTED {type(error).__name__}: {error}"
            finally:
                connection.close()
            with guard:
                outcomes.append(outcome)

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            list(pool.map(worker, jobs))
        return Counter(outcomes)

    def test_50_users_one_slot_exactly_one_winner(self):
        slot = self.make_slot(1)
        outcomes = self.hammer([(user, slot.id) for user in self.users])
        self.assertEqual(outcomes, Counter({"success": 1, "unavailable": 49}))
        self.assertEqual(Booking.objects.filter(slot=slot).count(), 1)

    def test_same_user_double_click_creates_only_one_booking(self):
        slot = self.make_slot(1)
        outcomes = self.hammer([(self.users[0], slot.id)] * 10)
        self.assertEqual(outcomes, Counter({"success": 1, "unavailable": 9}))

    def test_different_slots_do_not_block_each_other(self):
        """10 slots at the same venue, 5 users fighting for each: exactly 10 winners."""
        slots = [self.make_slot(h) for h in range(1, 11)]
        jobs = [(user, slots[i % 10].id) for i, user in enumerate(self.users)]
        outcomes = self.hammer(jobs)
        self.assertEqual(outcomes, Counter({"success": 10, "unavailable": 40}))
        for slot in slots:
            self.assertEqual(Booking.objects.filter(slot=slot).count(), 1)

    def test_stale_hold_under_concurrency_exactly_one_new_winner(self):
        slot = self.make_slot(1)
        Booking.objects.create(
            user=self.users[0], slot=slot, status=BookingStatus.PENDING,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        outcomes = self.hammer([(user, slot.id) for user in self.users[1:21]])
        self.assertEqual(outcomes, Counter({"success": 1, "unavailable": 19}))
        statuses = Counter(Booking.objects.filter(slot=slot).values_list("status", flat=True))
        self.assertEqual(statuses, Counter({"EXPIRED": 1, "PENDING": 1}))

    def test_booking_one_slot_does_not_lock_the_whole_venue(self):
        """Someone holds a lock on the VENUE row. Booking a slot of that venue must still work
        at once. This proves create_booking locks only the slot row (of=("self",)).
        Without of=("self",), select_related("venue") would also lock the venue, every booking
        at that venue would queue up, and this test would time out."""
        slot = self.make_slot(1)

        def book():
            try:
                services.create_booking(self.users[0], slot.id)
            finally:
                connection.close()

        with transaction.atomic():
            Venue.objects.select_for_update().get(pk=self.venue.pk)  # we now hold the venue lock
            thread = threading.Thread(target=book)
            thread.start()
            thread.join(timeout=5)
            finished_while_venue_was_locked = not thread.is_alive()
        thread.join(timeout=20)  # let the thread end in any case, so the test cleans up properly

        self.assertTrue(finished_while_venue_was_locked, "booking got stuck behind a venue lock")
        self.assertEqual(Booking.objects.filter(slot=slot).count(), 1)
