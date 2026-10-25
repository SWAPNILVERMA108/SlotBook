"""Live demo of the main feature: many users press "Book" on the SAME slot at the SAME moment,
over real HTTP, and exactly one of them wins.

    python manage.py runserver                         # terminal 1 (or: docker compose up)
    python manage.py race_demo --users 30              # terminal 2

Run it on camera for your README GIF or your interview demo.
(With Docker:  docker compose exec web python manage.py race_demo --base-url http://127.0.0.1:8000)

How it works: the command creates a throw-away venue, slot and users in the database, makes a
login token for every user (no password needed), fires all booking requests together, counts the
answers, checks the database, and removes everything it created.
"""

import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import requests
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from bookings.models import ACTIVE_STATUSES, Booking
from venues.models import Slot, Venue

User = get_user_model()


def post_booking(base_url, token, slot_id):
    """One user presses "Book". Returns the HTTP status code of the answer."""
    response = requests.post(
        f"{base_url.rstrip('/')}/api/bookings/",
        json={"slot_id": slot_id},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return response.status_code


class Command(BaseCommand):
    help = "Fires many simultaneous booking requests at one slot and shows that only one wins."

    def add_arguments(self, parser):
        parser.add_argument(
            "--users", type=int, default=30, help="how many users compete (default 30)"
        )
        parser.add_argument(
            "--base-url", default="http://127.0.0.1:8000", help="where the server runs"
        )
        parser.add_argument(
            "--keep", action="store_true", help="do not delete the demo data afterwards"
        )

    def handle(self, *args, **options):
        users_count = options["users"]
        if users_count < 2:
            raise CommandError("--users must be at least 2")
        base_url = options["base_url"]

        run_id = uuid4().hex[:8]
        owner = User.objects.create_user(f"race_owner_{run_id}")
        venue = Venue.objects.create(
            owner=owner, name=f"Race Demo {run_id}", address="Nowhere", city="Raceville"
        )
        start = timezone.now() + timedelta(days=2)
        slot = Slot.objects.create(
            venue=venue,
            start_time=start,
            end_time=start + timedelta(hours=1),
            price=Decimal("100.00"),
        )
        users = [User.objects.create_user(f"race_{run_id}_{i}") for i in range(users_count)]
        tokens = [str(RefreshToken.for_user(user).access_token) for user in users]

        try:
            self.stdout.write(
                f"{users_count} users press 'Book' on slot {slot.id} at the same moment..."
            )
            statuses, seconds = self.fire(base_url, tokens, slot.id)
            active = Booking.objects.filter(slot=slot, status__in=ACTIVE_STATUSES).count()
        finally:
            if not options["keep"]:
                self.clean_up(slot, venue, users, owner)

        booked = statuses.get(201, 0)
        taken = statuses.get(409, 0)
        other = users_count - booked - taken
        self.stdout.write(f"  HTTP 201 (booked):         {booked}")
        self.stdout.write(f"  HTTP 409 (already taken):  {taken}")
        self.stdout.write(
            f"  anything else:             {other}   {dict(statuses) if other else ''}"
        )
        self.stdout.write(f"  active bookings in the database for this slot: {active}")
        self.stdout.write(f"  time: {seconds:.2f} seconds")

        if booked == 1 and active == 1 and other == 0:
            self.stdout.write(
                self.style.SUCCESS("PASS: exactly one winner, everybody else got a clean 409.")
            )
        else:
            self.stdout.write(self.style.ERROR("FAIL: that is NOT the expected result."))
            raise CommandError("Race demo failed.")

    def fire(self, base_url, tokens, slot_id):
        barrier = threading.Barrier(len(tokens))  # nobody starts until everybody is ready

        def press(token):
            barrier.wait()
            try:
                return post_booking(base_url, token, slot_id)
            except requests.RequestException as error:
                return f"error: {type(error).__name__}"

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(tokens)) as pool:
            statuses = Counter(pool.map(press, tokens))
        return statuses, time.perf_counter() - started

    def clean_up(self, slot, venue, users, owner):
        Booking.objects.filter(slot=slot).delete()
        slot.delete()
        venue.delete()
        User.objects.filter(pk__in=[user.pk for user in users] + [owner.pk]).delete()
