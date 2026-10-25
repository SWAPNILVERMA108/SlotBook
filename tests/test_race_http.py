"""30 different users press "Book" for the SAME slot at the same moment, through the real HTTP API.

(bookings/tests/test_concurrency.py tests the service function with threads. This one goes
through the URL, the serializer and the error handling too.)
"""

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection
from rest_framework.test import APIClient

from bookings.models import ACTIVE_STATUSES, Booking
from tests.factories import SlotFactory, UserFactory

USERS = 30


@pytest.mark.django_db(transaction=True)  # real commits, so the threads can see each other
def test_only_one_of_30_simultaneous_http_requests_gets_the_slot():
    slot = SlotFactory()
    users = [UserFactory(password="x") for _ in range(USERS)]
    barrier = threading.Barrier(USERS)

    def press_book(user):
        client = APIClient()
        client.force_authenticate(user)
        barrier.wait()  # everybody starts at the same moment
        try:
            return client.post("/api/bookings/", {"slot_id": slot.id}, format="json").status_code
        finally:
            connection.close()  # each thread has its own database connection

    with ThreadPoolExecutor(max_workers=USERS) as pool:
        results = Counter(pool.map(press_book, users))

    assert results == {201: 1, 409: USERS - 1}  # one winner, 29 polite "already taken", NO 500s
    assert Booking.objects.filter(slot=slot, status__in=ACTIVE_STATUSES).count() == 1
