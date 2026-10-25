from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from bookings.models import Booking, BookingStatus
from venues.models import Slot, Venue

User = get_user_model()


class SlotListApiTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="Sl0t-Book!2026")
        self.user = User.objects.create_user("alice", password="Sl0t-Book!2026")
        self.turf = Venue.objects.create(owner=self.owner, name="City Turf", address="A", city="Ghaziabad")
        self.gym = Venue.objects.create(owner=self.owner, name="Iron Gym", address="B", city="Noida")
        self.base = (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        self.url = reverse("slot-list")
        self.client.force_authenticate(self.user)

    def make_slot(self, venue, hours_from_base, price="500", **extra):
        start = self.base + timedelta(hours=hours_from_base)
        return Slot.objects.create(
            venue=venue, start_time=start, end_time=start + timedelta(hours=1),
            price=Decimal(price), **extra,
        )

    def ids(self, response):
        return [row["id"] for row in response.data["results"]]

    def test_requires_login(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_lists_open_slots_in_time_order_with_pagination_fields(self):
        later = self.make_slot(self.turf, 5)
        earlier = self.make_slot(self.turf, 1)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        for key in ("count", "next", "previous", "results"):
            self.assertIn(key, response.data)
        self.assertEqual(self.ids(response), [earlier.id, later.id])

    def test_hides_past_inactive_and_inactive_venue_slots(self):
        visible = self.make_slot(self.turf, 1)
        past = Slot.objects.create(
            venue=self.turf, start_time=timezone.now() - timedelta(hours=3),
            end_time=timezone.now() - timedelta(hours=2), price=Decimal("100"),
        )
        inactive = self.make_slot(self.turf, 2, is_active=False)
        closed_venue = Venue.objects.create(owner=self.owner, name="Closed", address="C", city="Delhi", is_active=False)
        in_closed_venue = self.make_slot(closed_venue, 3)
        ids = self.ids(self.client.get(self.url))
        self.assertIn(visible.id, ids)
        for hidden in (past, inactive, in_closed_venue):
            self.assertNotIn(hidden.id, ids)

    def test_hides_slot_with_live_pending_hold_and_confirmed_booking(self):
        held = self.make_slot(self.turf, 1)
        sold = self.make_slot(self.turf, 2)
        free = self.make_slot(self.turf, 3)
        Booking.objects.create(user=self.user, slot=held, status=BookingStatus.PENDING,
                               expires_at=timezone.now() + timedelta(minutes=10))
        Booking.objects.create(user=self.user, slot=sold, status=BookingStatus.CONFIRMED)
        self.assertEqual(self.ids(self.client.get(self.url)), [free.id])

    def test_shows_slot_again_when_hold_has_ended_even_if_not_marked_expired_yet(self):
        slot = self.make_slot(self.turf, 1)
        Booking.objects.create(user=self.user, slot=slot, status=BookingStatus.PENDING,
                               expires_at=timezone.now() - timedelta(seconds=5))
        self.assertIn(slot.id, self.ids(self.client.get(self.url)))

    def test_shows_slot_again_after_cancel_or_expiry(self):
        slot = self.make_slot(self.turf, 1)
        Booking.objects.create(user=self.user, slot=slot, status=BookingStatus.CANCELLED)
        self.assertIn(slot.id, self.ids(self.client.get(self.url)))

    def test_filter_by_city_is_case_insensitive(self):
        turf_slot = self.make_slot(self.turf, 1)
        self.make_slot(self.gym, 2)
        response = self.client.get(self.url, {"city": "ghaziabad"})
        self.assertEqual(self.ids(response), [turf_slot.id])

    def test_filter_by_venue_and_price_range(self):
        cheap = self.make_slot(self.turf, 1, price="200")
        self.make_slot(self.turf, 2, price="800")
        self.make_slot(self.gym, 3, price="250")
        response = self.client.get(self.url, {"venue": self.turf.id, "max_price": "300"})
        self.assertEqual(self.ids(response), [cheap.id])

    def test_filter_by_date_uses_local_time(self):
        slot = self.make_slot(self.turf, 1)
        local_day = timezone.localtime(slot.start_time).date().isoformat()
        response = self.client.get(self.url, {"date": local_day})
        self.assertIn(slot.id, self.ids(response))
        other = (timezone.localtime(slot.start_time).date() + timedelta(days=30)).isoformat()
        self.assertEqual(self.ids(self.client.get(self.url, {"date": other})), [])

    def test_ordering_by_price_descending(self):
        a = self.make_slot(self.turf, 1, price="100")
        b = self.make_slot(self.turf, 2, price="900")
        response = self.client.get(self.url, {"ordering": "-price"})
        self.assertEqual(self.ids(response), [b.id, a.id])

    def test_no_n_plus_one_queries(self):
        for i in range(8):
            self.make_slot(self.turf, i + 1)
        # auth is forced, so the count is: 1 COUNT query + 1 slots query (venue is JOINed)
        with self.assertNumQueries(2):
            self.client.get(self.url)

    def test_venue_list(self):
        response = self.client.get(reverse("venue-list"))
        names = [v["name"] for v in response.data["results"]]
        self.assertCountEqual(names, ["City Turf", "Iron Gym"])
