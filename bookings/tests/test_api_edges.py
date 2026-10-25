"""DAY 7 tests: edge cases of the API (bad input, pagination, filters, safe error answers)."""
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from bookings.models import Booking, BookingStatus
from venues.models import Slot, Venue

User = get_user_model()


class ApiEdgeCaseTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.alice = User.objects.create_user("alice")
        self.venue = Venue.objects.create(owner=self.owner, name="City Turf", address="A", city="Ghaziabad")
        self.base = (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        self.client.force_authenticate(self.alice)

    def make_slots(self, count):
        return [
            Slot.objects.create(
                venue=self.venue,
                start_time=self.base + timedelta(hours=i),
                end_time=self.base + timedelta(hours=i + 1),
                price=Decimal("100"),
            )
            for i in range(count)
        ]

    # ---- pagination ----
    def test_default_page_size_is_10(self):
        self.make_slots(25)
        response = self.client.get(reverse("slot-list"))
        self.assertEqual(response.data["count"], 25)
        self.assertEqual(len(response.data["results"]), 10)
        self.assertIsNotNone(response.data["next"])

    def test_client_can_choose_page_size(self):
        self.make_slots(12)
        response = self.client.get(reverse("slot-list"), {"page_size": 4})
        self.assertEqual(len(response.data["results"]), 4)

    def test_page_size_is_capped_at_50(self):
        self.make_slots(60)
        response = self.client.get(reverse("slot-list"), {"page_size": 1000})
        self.assertEqual(len(response.data["results"]), 50)

    def test_second_page_continues_where_first_ended(self):
        slots = self.make_slots(15)
        page2 = self.client.get(reverse("slot-list"), {"page": 2})
        self.assertEqual([s["id"] for s in page2.data["results"]], [s.id for s in slots[10:]])

    def test_page_beyond_the_end_is_404(self):
        self.make_slots(3)
        self.assertEqual(self.client.get(reverse("slot-list"), {"page": 99}).status_code, 404)

    def test_bad_page_value_is_404_not_500(self):
        self.assertEqual(self.client.get(reverse("slot-list"), {"page": "abc"}).status_code, 404)

    def test_bad_page_size_falls_back_to_default(self):
        self.make_slots(12)
        response = self.client.get(reverse("slot-list"), {"page_size": "abc"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 10)

    # ---- filters with garbage input ----
    def test_invalid_date_filter_is_400_not_500(self):
        response = self.client.get(reverse("slot-list"), {"date": "not-a-date"})
        self.assertEqual(response.status_code, 400)

    def test_invalid_price_filter_is_400(self):
        response = self.client.get(reverse("slot-list"), {"min_price": "cheap"})
        self.assertEqual(response.status_code, 400)

    def test_unknown_ordering_field_is_ignored_safely(self):
        self.make_slots(2)
        response = self.client.get(reverse("slot-list"), {"ordering": "password"})
        self.assertEqual(response.status_code, 200)

    def test_booking_status_filter_with_unknown_value_is_400(self):
        response = self.client.get(reverse("booking-list"), {"status": "NONSENSE"})
        self.assertEqual(response.status_code, 400)

    # ---- bad ids in the URL ----
    def test_cancel_with_non_numeric_id_is_404_not_500(self):
        response = self.client.post("/api/bookings/abc/cancel/")
        self.assertEqual(response.status_code, 404)

    def test_detail_with_non_numeric_id_is_404(self):
        self.assertEqual(self.client.get("/api/bookings/abc/").status_code, 404)
        self.assertEqual(self.client.get("/api/slots/abc/").status_code, 404)

    def test_huge_ids_do_not_crash(self):
        huge = 10**30
        self.assertEqual(self.client.get(f"/api/slots/{huge}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/bookings/{huge}/").status_code, 404)
        self.assertEqual(self.client.post(f"/api/bookings/{huge}/cancel/").status_code, 404)
        response = self.client.post(reverse("booking-list"), {"slot_id": huge}, format="json")
        self.assertIn(response.status_code, (400, 404))

    # ---- booking input ----
    def test_slot_id_must_be_a_whole_number(self):
        for bad in ("1.5", None, [], {}, True):
            response = self.client.post(reverse("booking-list"), {"slot_id": bad}, format="json")
            self.assertEqual(response.status_code, 400, bad)

    # ---- my bookings pagination ----
    def test_my_bookings_are_paginated_newest_first(self):
        for slot in self.make_slots(12):
            Booking.objects.create(user=self.alice, slot=slot, status=BookingStatus.CONFIRMED)
        response = self.client.get(reverse("booking-me"))
        self.assertEqual(response.data["count"], 12)
        self.assertEqual(len(response.data["results"]), 10)
        ids = [b["id"] for b in response.data["results"]]
        self.assertEqual(ids, sorted(ids, reverse=True))


class StringOutputTests(APITestCase):
    def test_slot_str_shows_local_time_not_utc(self):
        owner = User.objects.create_user("owner")
        venue = Venue.objects.create(owner=owner, name="City Turf", address="A", city="Noida")
        # 12:30 UTC is 18:00 in Asia/Kolkata (UTC+5:30)
        start = datetime(2030, 1, 15, 12, 30, tzinfo=dt_timezone.utc)
        slot = Slot.objects.create(
            venue=venue, start_time=start, end_time=start + timedelta(hours=1), price=Decimal("1"),
        )
        slot.refresh_from_db()  # as loaded from the database (UTC)
        self.assertEqual(str(slot), "City Turf | 15 Jan 2030 18:00")
