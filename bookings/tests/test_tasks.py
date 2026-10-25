"""DAY 8 tests: Celery wiring. These run WITHOUT Redis, because tasks run 'eagerly'
(directly inside the test, no worker needed). A real worker test is a manual step in the guide."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bookings import tasks
from bookings.models import Booking, BookingStatus
from config.celery import app as celery_app
from venues.models import Slot, Venue

User = get_user_model()


class EagerCeleryMixin:
    def setUp(self):
        super().setUp()
        self._old = (celery_app.conf.task_always_eager, celery_app.conf.task_eager_propagates)
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True  # errors inside a task fail the test loudly
        self.addCleanup(self.restore)

    def restore(self):
        celery_app.conf.task_always_eager, celery_app.conf.task_eager_propagates = self._old


class CeleryWiringTests(EagerCeleryMixin, TestCase):
    def test_celery_app_is_loaded_with_django(self):
        import config

        self.assertIs(config.celery_app, celery_app)

    def test_tasks_are_discovered(self):
        celery_app.loader.import_default_modules()
        self.assertIn("bookings.tasks.ping", celery_app.tasks)
        self.assertIn("bookings.tasks.report_pending_bookings", celery_app.tasks)

    def test_settings_were_read_from_django_settings(self):
        self.assertEqual(celery_app.conf.task_serializer, "json")
        self.assertEqual(list(celery_app.conf.accept_content), ["json"])
        self.assertEqual(celery_app.conf.timezone, "Asia/Kolkata")
        self.assertTrue(str(celery_app.conf.broker_url).startswith("redis://"))

    def test_ping_returns_pong(self):
        self.assertEqual(tasks.ping.delay().get(), "pong")


class ReportPendingBookingsTests(EagerCeleryMixin, TestCase):
    def setUp(self):
        super().setUp()
        owner = User.objects.create_user("owner")
        self.user = User.objects.create_user("alice")
        venue = Venue.objects.create(owner=owner, name="City Turf", address="A", city="Noida")
        base = timezone.now() + timedelta(days=1)
        self.slots = [
            Slot.objects.create(
                venue=venue, start_time=base + timedelta(hours=i),
                end_time=base + timedelta(hours=i + 1), price=Decimal("100"),
            )
            for i in range(4)
        ]

    def make(self, slot, status, minutes_left=10):
        return Booking.objects.create(
            user=self.user, slot=slot, status=status,
            expires_at=timezone.now() + timedelta(minutes=minutes_left),
        )

    def test_empty_database(self):
        self.assertEqual(tasks.report_pending_bookings.delay().get(), {"pending": 0, "overdue": 0})

    def test_counts_pending_and_overdue_only(self):
        self.make(self.slots[0], BookingStatus.PENDING, minutes_left=5)    # live hold
        self.make(self.slots[1], BookingStatus.PENDING, minutes_left=-5)   # overdue
        self.make(self.slots[2], BookingStatus.CONFIRMED)                  # ignored
        self.make(self.slots[3], BookingStatus.EXPIRED)                    # ignored
        self.assertEqual(tasks.report_pending_bookings.delay().get(), {"pending": 2, "overdue": 1})

    def test_task_only_reads_it_changes_nothing(self):
        overdue = self.make(self.slots[0], BookingStatus.PENDING, minutes_left=-5)
        tasks.report_pending_bookings.delay().get()
        overdue.refresh_from_db()
        self.assertEqual(overdue.status, BookingStatus.PENDING)

    def test_result_is_json_serialisable(self):
        import json

        json.dumps(tasks.report_pending_bookings.delay().get())


class HealthCheckTests(TestCase):
    def test_health_reports_database_and_redis_keys(self):
        response = self.client.get(reverse("health-check"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["database"], "connected")
        self.assertIn(response.json()["redis"], ("connected", "unavailable"))

    def test_health_stays_200_when_redis_is_down(self):
        with mock.patch("config.urls.redis_status", return_value="unavailable"):
            response = self.client.get(reverse("health-check"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["redis"], "unavailable")

    def test_health_is_503_when_database_is_down(self):
        with mock.patch("config.urls.connection") as broken:
            broken.cursor.side_effect = Exception("db is down")
            response = self.client.get(reverse("health-check"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(response.json()["database"], "unavailable")

    def test_health_needs_no_login(self):
        self.assertEqual(self.client.get(reverse("health-check")).status_code, 200)
