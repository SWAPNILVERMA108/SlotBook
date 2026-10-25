"""DAY 7: the admin list pages must not run one extra query per row (the N+1 problem)."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from bookings.models import Booking, BookingStatus
from payments.models import Payment
from venues.models import Slot, Venue

User = get_user_model()


class AdminQueryCountTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("boss", "boss@example.com", "pass12345")
        cls.owner = User.objects.create_user("owner")
        cls.venue = Venue.objects.create(owner=cls.owner, name="City Turf", address="A", city="Ghaziabad")

    def add_rows(self, how_many, offset):
        for i in range(how_many):
            user = User.objects.create_user(f"u{offset}_{i}")
            start = timezone.now() + timedelta(days=1, minutes=offset + i)
            slot = Slot.objects.create(
                venue=self.venue, start_time=start, end_time=start + timedelta(minutes=30),
                price=Decimal("100"),
            )
            booking = Booking.objects.create(user=user, slot=slot, status=BookingStatus.CONFIRMED)
            Payment.objects.create(booking=booking, razorpay_order_id=f"order_{offset}_{i}", amount=Decimal("100"))

    def queries_for(self, url_name):
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse(url_name))
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def assert_constant_queries(self, url_name):
        self.add_rows(3, offset=0)
        few = self.queries_for(url_name)
        self.add_rows(12, offset=100)
        many = self.queries_for(url_name)
        self.assertEqual(few, many, f"{url_name}: {few} queries for 3 rows but {many} for 15 rows (N+1!)")

    def test_booking_list_has_constant_query_count(self):
        self.assert_constant_queries("admin:bookings_booking_changelist")

    def test_slot_list_has_constant_query_count(self):
        self.assert_constant_queries("admin:venues_slot_changelist")

    def test_payment_list_has_constant_query_count(self):
        self.assert_constant_queries("admin:payments_payment_changelist")
