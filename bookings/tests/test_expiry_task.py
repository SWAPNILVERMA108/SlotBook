"""DAY 9: the Celery task that frees slots whose hold has ended."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from bookings import services, tasks
from bookings.models import Booking, BookingStatus
from config.celery import app as celery_app
from venues.models import Slot, Venue

User = get_user_model()


class ExpiryFixtureMixin:
    def make_world(self):
        self.owner = User.objects.create_user("owner")
        self.users = [User.objects.create_user(f"u{i}") for i in range(30)]
        self.venue = Venue.objects.create(
            owner=self.owner, name="City Turf", address="A", city="Noida"
        )
        self.counter = 0

    def make_slot(self):
        self.counter += 1
        start = timezone.now() + timedelta(days=1, minutes=self.counter * 30)
        return Slot.objects.create(
            venue=self.venue,
            start_time=start,
            end_time=start + timedelta(minutes=30),
            price=Decimal("100"),
        )

    def make_booking(self, user, status, minutes_left=None, slot=None):
        expires = (
            timezone.now() + timedelta(minutes=minutes_left) if minutes_left is not None else None
        )
        return Booking.objects.create(
            user=user, slot=slot or self.make_slot(), status=status, expires_at=expires
        )


class ReleaseExpiredBookingsTests(ExpiryFixtureMixin, TestCase):
    def setUp(self):
        self.make_world()

    def test_expires_only_pending_bookings_whose_hold_has_ended(self):
        overdue = self.make_booking(self.users[0], BookingStatus.PENDING, minutes_left=-1)
        live = self.make_booking(self.users[1], BookingStatus.PENDING, minutes_left=5)
        confirmed = self.make_booking(self.users[2], BookingStatus.CONFIRMED)
        cancelled = self.make_booking(self.users[3], BookingStatus.CANCELLED)
        already = self.make_booking(self.users[4], BookingStatus.EXPIRED)

        self.assertEqual(tasks.release_expired_bookings(), 1)

        for booking, expected in (
            (overdue, "EXPIRED"),
            (live, "PENDING"),
            (confirmed, "CONFIRMED"),
            (cancelled, "CANCELLED"),
            (already, "EXPIRED"),
        ):
            booking.refresh_from_db()
            self.assertEqual(booking.status, expected, booking)

    def test_is_idempotent_second_run_does_nothing(self):
        self.make_booking(self.users[0], BookingStatus.PENDING, minutes_left=-1)
        self.assertEqual(tasks.release_expired_bookings(), 1)
        stamp = Booking.objects.get().updated_at
        self.assertEqual(tasks.release_expired_bookings(), 0)
        self.assertEqual(tasks.release_expired_bookings(), 0)
        self.assertEqual(Booking.objects.get().updated_at, stamp)

    def test_updates_updated_at_because_update_skips_auto_now(self):
        booking = self.make_booking(self.users[0], BookingStatus.PENDING, minutes_left=-1)
        before = booking.updated_at
        tasks.release_expired_bookings()
        booking.refresh_from_db()
        self.assertGreater(booking.updated_at, before)

    def test_does_nothing_on_an_empty_database(self):
        self.assertEqual(tasks.release_expired_bookings(), 0)

    def test_expires_many_at_once(self):
        for i in range(25):
            self.make_booking(self.users[i], BookingStatus.PENDING, minutes_left=-5)
        self.assertEqual(tasks.release_expired_bookings(), 25)
        self.assertFalse(Booking.objects.filter(status=BookingStatus.PENDING).exists())

    def test_the_freed_slot_can_be_booked_again(self):
        old = self.make_booking(self.users[0], BookingStatus.PENDING, minutes_left=-1)
        tasks.release_expired_bookings()
        new = services.create_booking(self.users[1], old.slot_id)
        self.assertEqual(new.status, BookingStatus.PENDING)

    def test_hold_that_ends_exactly_now_counts_as_expired(self):
        booking = self.make_booking(self.users[0], BookingStatus.PENDING, minutes_left=0)
        self.assertEqual(tasks.release_expired_bookings(), 1)
        booking.refresh_from_db()
        self.assertEqual(booking.status, BookingStatus.EXPIRED)

    def test_task_returns_a_plain_int_so_celery_can_store_it(self):
        self.assertIsInstance(tasks.release_expired_bookings(), int)


class BeatScheduleTests(TestCase):
    def test_task_is_registered_and_scheduled_every_minute(self):
        celery_app.loader.import_default_modules()
        self.assertIn("bookings.tasks.release_expired_bookings", celery_app.tasks)
        entry = settings.CELERY_BEAT_SCHEDULE["release-expired-bookings"]
        self.assertEqual(entry["task"], "bookings.tasks.release_expired_bookings")
        self.assertEqual(entry["schedule"], 60.0)

    def test_refund_sweeper_is_scheduled_and_registered(self):
        celery_app.loader.import_default_modules()
        entry = settings.CELERY_BEAT_SCHEDULE["process-pending-refunds"]
        self.assertIn(entry["task"], celery_app.tasks)
        self.assertEqual(entry["schedule"], 300.0)

    def test_task_retries_on_database_trouble(self):
        from django.db import OperationalError

        self.assertIn(OperationalError, tasks.release_expired_bookings.autoretry_for)


class ReleaseTaskRaceTests(ExpiryFixtureMixin, TransactionTestCase):
    """The task runs in the background while users keep booking. Nothing may break."""

    def setUp(self):
        self.make_world()

    def run_together(self, jobs):
        barrier = threading.Barrier(len(jobs))
        results = []

        def worker(job):
            barrier.wait()
            try:
                results.append(job())
            except Exception as error:  # noqa: BLE001
                results.append(error)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            list(pool.map(worker, jobs))
        return results

    def test_task_and_20_users_racing_for_a_stale_slot_give_exactly_one_winner(self):
        for round_number in range(5):
            slot = self.make_slot()
            self.make_booking(
                self.owner, BookingStatus.PENDING, minutes_left=-1, slot=slot
            )  # stale hold
            jobs = [tasks.release_expired_bookings]
            jobs += [
                lambda user=user, slot_id=slot.id: services.create_booking(user, slot_id)
                for user in self.users[:20]
            ]

            results = self.run_together(jobs)

            unexpected = [
                r
                for r in results
                if isinstance(r, Exception) and not isinstance(r, services.SlotUnavailable)
            ]
            self.assertEqual(unexpected, [], f"round {round_number}")
            winners = [r for r in results if isinstance(r, Booking)]
            self.assertEqual(len(winners), 1, f"round {round_number}")
            active = Booking.objects.filter(
                slot=slot, status__in=[BookingStatus.PENDING, BookingStatus.CONFIRMED]
            )
            self.assertEqual(active.count(), 1)

    def test_two_tasks_running_at_the_same_time_expire_each_booking_once(self):
        for i in range(10):
            self.make_booking(self.users[i], BookingStatus.PENDING, minutes_left=-1)
        results = self.run_together([tasks.release_expired_bookings] * 4)
        self.assertFalse([r for r in results if isinstance(r, Exception)])
        self.assertEqual(sum(results), 10)  # each booking was expired by exactly one of them
