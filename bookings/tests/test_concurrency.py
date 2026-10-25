"""DAY 5: PROVE the race condition with 50 users clicking "Book" at the same moment.

Run only this file:   python manage.py test bookings.tests.test_concurrency -v 2

Expected results TODAY (Day 5):
  test_1  PASSES  -> shows the naive code failing messily (raw database errors)
  test_2  PASSES  -> shows REAL double booking when the database safety net is removed
  test_3  FAILS   -> this is the target. It turns green on DAY 6 after you fix create_booking().

Why TransactionTestCase and not TestCase?  TestCase wraps each test in one big transaction
that other threads cannot see. Real concurrency needs real, committed transactions.
Why connection.close() in every thread?  Each thread opens its own database connection, and
leaving them open would stop Django from deleting the test database afterwards.
"""
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from functools import partial

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from bookings import services
from bookings.models import ACTIVE_STATUSES, Booking
from venues.models import Slot, Venue

User = get_user_model()

THREADS = 50


class RaceConditionTests(TransactionTestCase):
    def setUp(self):
        # No password given = no slow password hashing, so creating 50 users is quick.
        owner = User.objects.create_user("owner")
        self.users = [User.objects.create_user(f"user{i}") for i in range(THREADS)]
        venue = Venue.objects.create(owner=owner, name="City Turf", address="A", city="Ghaziabad")
        start = timezone.now() + timedelta(days=1)
        self.slot = Slot.objects.create(
            venue=venue, start_time=start, end_time=start + timedelta(hours=1), price=Decimal("500"),
        )

    # ---------- helper: fire THREADS requests at the same moment ----------
    def hammer(self, book_function):
        """Runs book_function(user, slot_id) in 50 threads at once. Returns a Counter of outcomes."""
        barrier = threading.Barrier(THREADS, timeout=60)  # nobody starts until ALL 50 are ready
        outcomes = []
        guard = threading.Lock()

        def worker(user):
            # Open this thread's database connection BEFORE the start signal. Opening a
            # connection takes a few milliseconds, and without this the threads would start
            # at slightly different times, which makes the race flaky.
            try:
                connection.ensure_connection()
            except Exception:
                pass  # the real call below will fail loudly and be counted as UNEXPECTED
            barrier.wait()
            try:
                book_function(user, self.slot.id)
                outcome = "success"
            except services.SlotUnavailable:
                outcome = "clean 'slot unavailable'"
            except Exception as error:  # anything else = a bug in our logic
                outcome = f"UNEXPECTED {type(error).__name__}"
            finally:
                connection.close()  # close THIS thread's database connection
            with guard:
                outcomes.append(outcome)

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            list(pool.map(worker, self.users))
        return Counter(outcomes)

    def active_bookings(self):
        return Booking.objects.filter(slot=self.slot, status__in=ACTIVE_STATUSES).count()

    # ---------- test 1: naive code + database safety net ----------
    def test_1_naive_code_fails_messily_under_concurrency(self):
        naive = partial(services.create_booking_naive, _pause=0.1)  # widen the race window
        outcomes = self.hammer(naive)
        print(f"\n[test_1] outcomes of 50 simultaneous bookings: {dict(outcomes)}")

        unexpected = sum(n for name, n in outcomes.items() if name.startswith("UNEXPECTED"))
        # The unique constraint from Day 2 saved us from double booking...
        self.assertEqual(self.active_bookings(), 1)
        # ...but the naive code did not handle it gracefully: raw errors leaked out.
        self.assertGreater(unexpected, 0, "expected raw IntegrityErrors from the naive version")

    # ---------- test 2: naive code WITHOUT the safety net = real double booking ----------
    def test_2_naive_code_double_books_without_the_database_constraint(self):
        constraint = next(c for c in Booking._meta.constraints if c.name == "one_active_booking_per_slot")
        with connection.schema_editor() as editor:
            editor.remove_constraint(Booking, constraint)

        def put_constraint_back():
            Booking.objects.all().delete()  # duplicates would block re-adding the constraint
            with connection.schema_editor() as editor:
                editor.add_constraint(Booking, constraint)

        self.addCleanup(put_constraint_back)

        naive = partial(services.create_booking_naive, _pause=0.1)
        outcomes = self.hammer(naive)
        print(f"\n[test_2] outcomes WITHOUT the DB constraint: {dict(outcomes)}")
        print(f"[test_2] active bookings for ONE slot: {self.active_bookings()}  <-- this is the bug")

        self.assertGreater(self.active_bookings(), 1, "expected DOUBLE BOOKING from the naive version")

    # ---------- test 3: the target for Day 6 (RED today, GREEN after your fix) ----------
    def test_3_create_booking_is_safe_under_concurrency(self):
        outcomes = self.hammer(services.create_booking)
        print(f"\n[test_3] outcomes of create_booking: {dict(outcomes)}")

        self.assertEqual(outcomes["success"], 1, "exactly ONE user must win the slot")
        self.assertEqual(outcomes["clean 'slot unavailable'"], THREADS - 1, "everyone else gets a clean error")
        self.assertEqual(self.active_bookings(), 1)
