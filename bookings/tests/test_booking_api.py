"""DAY 4 tests: the booking API (create, list, cancel, permissions)."""
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


class BookingApiTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="Sl0t-Book!2026")
        self.alice = User.objects.create_user("alice", password="Sl0t-Book!2026")
        self.bob = User.objects.create_user("bob", password="Sl0t-Book!2026")
        self.venue = Venue.objects.create(owner=self.owner, name="City Turf", address="A", city="Ghaziabad")
        self.slot = self.make_slot(2)
        self.list_url = reverse("booking-list")
        self.client.force_authenticate(self.alice)

    def make_slot(self, hours_from_now, **extra):
        start = timezone.now() + timedelta(hours=hours_from_now)
        return Slot.objects.create(
            venue=self.venue, start_time=start, end_time=start + timedelta(hours=1),
            price=Decimal("500"), **extra,
        )

    def book(self, slot=None):
        return self.client.post(self.list_url, {"slot_id": (slot or self.slot).id}, format="json")

    # ---- create ----
    def test_requires_login(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.book().status_code, status.HTTP_401_UNAUTHORIZED)

    def test_book_a_slot_creates_pending_booking_with_10_minute_hold(self):
        response = self.book()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], "PENDING")
        booking = Booking.objects.get(pk=response.data["id"])
        self.assertEqual(booking.user, self.alice)
        hold = booking.expires_at - timezone.now()
        self.assertTrue(timedelta(minutes=9) < hold <= timedelta(minutes=10))

    def test_second_user_gets_409_for_a_held_slot(self):
        self.book()
        self.client.force_authenticate(self.bob)
        response = self.book()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(Booking.objects.filter(slot=self.slot).count(), 1)

    def test_unknown_slot_is_404(self):
        response = self.client.post(self.list_url, {"slot_id": 999999}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_inactive_and_past_slots_cannot_be_booked(self):
        inactive = self.make_slot(3, is_active=False)
        past = Slot.objects.create(
            venue=self.venue, start_time=timezone.now() - timedelta(hours=2),
            end_time=timezone.now() - timedelta(hours=1), price=Decimal("100"),
        )
        self.assertEqual(self.book(inactive).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.book(past).status_code, status.HTTP_404_NOT_FOUND)

    def test_bad_input_is_400(self):
        for body in ({}, {"slot_id": "abc"}, {"slot_id": 0}):
            response = self.client.post(self.list_url, body, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, body)

    # ---- list / detail ----
    def test_list_shows_only_my_bookings(self):
        mine = self.book().data["id"]
        other_slot = self.make_slot(4)
        Booking.objects.create(
            user=self.bob, slot=other_slot, status=BookingStatus.PENDING,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        for url in (self.list_url, reverse("booking-me")):
            response = self.client.get(url)
            self.assertEqual([b["id"] for b in response.data["results"]], [mine])

    def test_filter_my_bookings_by_status(self):
        self.book()
        response = self.client.get(self.list_url, {"status": "CANCELLED"})
        self.assertEqual(response.data["count"], 0)

    def test_cannot_open_someone_elses_booking(self):
        booking_id = self.book().data["id"]
        self.client.force_authenticate(self.bob)
        response = self.client.get(reverse("booking-detail", args=[booking_id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # ---- cancel ----
    def test_cancel_frees_the_slot_for_someone_else(self):
        booking_id = self.book().data["id"]
        cancel = self.client.post(reverse("booking-cancel", args=[booking_id]))
        self.assertEqual(cancel.status_code, status.HTTP_200_OK)
        self.assertEqual(cancel.data["status"], "CANCELLED")
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.book().status_code, status.HTTP_201_CREATED)

    def test_cannot_cancel_someone_elses_booking(self):
        booking_id = self.book().data["id"]
        self.client.force_authenticate(self.bob)
        response = self.client.post(reverse("booking-cancel", args=[booking_id]))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(Booking.objects.get(pk=booking_id).status, BookingStatus.PENDING)

    def test_cannot_cancel_twice(self):
        booking_id = self.book().data["id"]
        url = reverse("booking-cancel", args=[booking_id])
        self.client.post(url)
        self.assertEqual(self.client.post(url).status_code, status.HTTP_409_CONFLICT)

    def test_cannot_cancel_after_slot_started(self):
        booking = Booking.objects.create(
            user=self.alice, slot=self.make_slot(1), status=BookingStatus.CONFIRMED,
        )
        Slot.objects.filter(pk=booking.slot_id).update(
            start_time=timezone.now() - timedelta(minutes=5),
            end_time=timezone.now() + timedelta(minutes=55),
        )
        response = self.client.post(reverse("booking-cancel", args=[booking.id]))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_listing_does_not_run_one_query_per_booking(self):
        for i in range(6):
            Booking.objects.create(
                user=self.alice, slot=self.make_slot(10 + i), status=BookingStatus.CONFIRMED,
            )
        with self.assertNumQueries(2):  # 1 COUNT + 1 bookings query (slot and venue JOINed)
            self.client.get(self.list_url)
