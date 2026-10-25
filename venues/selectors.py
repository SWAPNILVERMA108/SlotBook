from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from bookings.models import Booking, BookingStatus
from venues.models import Slot


def available_slots():
    """Slots a customer can still book right now.

    A slot is hidden when someone holds it:
      - a CONFIRMED booking, or
      - a PENDING booking whose 10-minute hold has NOT ended yet.
    A PENDING booking whose hold already ended no longer hides the slot, even if the
    Celery task has not marked it EXPIRED yet (that task only runs once a minute).
    """
    now = timezone.now()
    holding = Booking.objects.filter(slot=OuterRef("pk")).filter(
        Q(status=BookingStatus.CONFIRMED)
        | Q(status=BookingStatus.PENDING, expires_at__gt=now)
    )
    return (
        Slot.objects.filter(is_active=True, venue__is_active=True, start_time__gt=now)
        .annotate(is_held=Exists(holding))
        .filter(is_held=False)
        .select_related("venue")  # one JOIN instead of one extra query per slot (N+1)
    )
