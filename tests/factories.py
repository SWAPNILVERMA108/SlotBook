"""factory_boy "factories": one line creates a valid object, with sensible random values.

    user = UserFactory()
    slot = SlotFactory(price=Decimal("250"))              # change only what the test cares about
    booking = BookingFactory(status="CONFIRMED")          # slot, user and venue are made for you

This keeps each test short and focused on the ONE thing it checks.
"""

from datetime import timedelta
from decimal import Decimal

import factory
from django.contrib.auth import get_user_model
from django.utils import timezone

from bookings.models import Booking, BookingStatus
from payments.models import Payment
from venues.models import Slot, Venue

PASSWORD = "Sl0t-Book!2026"


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = get_user_model()

    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda user: f"{user.username}@example.com")
    password = factory.django.Password(PASSWORD)  # stored hashed, like a real password


class VenueFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Venue

    owner = factory.SubFactory(UserFactory)
    name = factory.Sequence(lambda n: f"Venue {n}")
    address = "Sector 62"
    city = "Noida"


class SlotFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Slot

    venue = factory.SubFactory(VenueFactory)
    # Each slot starts an hour later than the previous one, so slots never collide.
    start_time = factory.Sequence(lambda n: timezone.now() + timedelta(days=1, hours=n))
    end_time = factory.LazyAttribute(lambda slot: slot.start_time + timedelta(minutes=30))
    price = Decimal("500.00")


class BookingFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Booking

    class Params:
        is_pending = factory.LazyAttribute(lambda booking: booking.status == BookingStatus.PENDING)

    user = factory.SubFactory(UserFactory)
    slot = factory.SubFactory(SlotFactory)
    status = BookingStatus.PENDING
    # Only a PENDING booking has a hold time (the database rule requires it).
    expires_at = factory.Maybe(
        "is_pending",
        yes_declaration=factory.LazyFunction(lambda: timezone.now() + timedelta(minutes=10)),
        no_declaration=None,
    )


class PaymentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Payment

    booking = factory.SubFactory(BookingFactory)
    razorpay_order_id = factory.Sequence(lambda n: f"order_F{n}")
    amount = Decimal("500.00")
