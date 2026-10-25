"""Booking business rules live here, NOT in views.

Why? Views should only talk HTTP (read request, send response). Keeping the rules in plain
functions makes them easy to test, and the Celery tasks and the payment webhook can call
the very same functions.

DAY 6: create_booking() is now SAFE (row lock). create_booking_naive() is kept on purpose,
as the "before" picture for the Day 5 experiment and for your README story.
"""

import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from venues.models import Slot

from .models import ACTIVE_STATUSES, Booking, BookingStatus

logger = logging.getLogger(__name__)


# ---------- our own error types (the view turns them into clean HTTP answers) ----------
class BookingError(Exception):
    """Base class for all expected booking problems."""


class SlotNotFound(BookingError):
    """Slot does not exist, is switched off, or has already started."""


class SlotUnavailable(BookingError):
    """Somebody else already holds or owns this slot."""


class BookingNotFound(BookingError):
    """No such booking for this user."""


class BookingNotCancellable(BookingError):
    """Booking is already cancelled/expired, or the slot has already started."""


def hold_minutes():
    return getattr(settings, "BOOKING_HOLD_MINUTES", 10)


# ---------- the NAIVE version (kept on purpose, has a race condition) ----------
def create_booking_naive(user, slot_id, _pause=0.0):
    """CHECK first, then CREATE. Looks fine, but it is NOT safe when many people click together.

    Picture two users, A and B, at the same moment:
        A: is the slot free?  -> yes
        B: is the slot free?  -> yes      (A has not created anything yet!)
        A: create booking
        B: create booking     -> both believed it was free

    `_pause` only exists for the Day 5 experiment: it widens the gap between CHECK and
    CREATE so the bug shows up every single time instead of sometimes.
    """
    try:
        slot = Slot.objects.select_related("venue").get(
            pk=slot_id,
            is_active=True,
            venue__is_active=True,
            start_time__gt=timezone.now(),
        )
    except Slot.DoesNotExist:
        raise SlotNotFound("This slot does not exist or is not open for booking.") from None

    # STEP 1: CHECK
    already_taken = Booking.objects.filter(slot=slot, status__in=ACTIVE_STATUSES).exists()
    if already_taken:
        raise SlotUnavailable("This slot has just been booked by someone else.")

    if _pause:
        time.sleep(_pause)

    # STEP 2: CREATE (anyone else may have created one between step 1 and now!)
    return Booking.objects.create(
        user=user,
        slot=slot,
        status=BookingStatus.PENDING,
        expires_at=timezone.now() + timedelta(minutes=hold_minutes()),
    )


# ---------- DAY 6: the SAFE version ----------
@transaction.atomic
def create_booking(user, slot_id):
    """Book a slot for `user`. Safe when many people click at the same moment.

    The idea: LOCK the slot row first, and only then CHECK and CREATE. Anyone else who wants
    the same slot must wait at the lock until we commit, and then sees our booking.

        A: lock slot -> check (free) -> create -> COMMIT (lock released)
        B: lock slot ........ waits ........... -> check (taken!) -> SlotUnavailable
    """
    # 1. LOCK. Everything below runs while we hold the slot's row lock.
    #    of=("self",) locks ONLY the slot row. Without it, select_related("venue") would also
    #    lock the venue row, and then all bookings at one venue would queue behind each other.
    try:
        slot = (
            Slot.objects.select_for_update(of=("self",))
            .select_related("venue")
            .get(
                pk=slot_id,
                is_active=True,
                venue__is_active=True,
                start_time__gt=timezone.now(),
            )
        )
    except Slot.DoesNotExist:
        raise SlotNotFound("This slot does not exist or is not open for booking.") from None

    # Take the time AFTER the lock: we may have waited, so an earlier "now" would be stale.
    now = timezone.now()

    # 2. CHECK: is somebody holding it for real?
    #    - a CONFIRMED booking, or
    #    - a PENDING booking whose hold has not ended yet.
    #    We do all READS before any WRITE, so raising here never throws away earlier work.
    live_holder = Booking.objects.filter(slot=slot).filter(
        Q(status=BookingStatus.CONFIRMED) | Q(status=BookingStatus.PENDING, expires_at__gt=now)
    )
    if live_holder.exists():
        raise SlotUnavailable("This slot has just been booked by someone else.")

    # 3. Any PENDING booking still on this slot is STALE (its hold already ended, but the
    #    Celery task has not marked it EXPIRED yet). Expire it now. If we did not, it would
    #    still trip the database's "one active booking per slot" rule.
    #    .update() skips auto_now, so we set updated_at ourselves.
    stale = Booking.objects.filter(slot=slot, status=BookingStatus.PENDING).update(
        status=BookingStatus.EXPIRED, updated_at=now
    )
    if stale:
        logger.info("Expired %s stale hold(s) on slot %s while booking it", stale, slot.pk)

    # 4. CREATE
    return Booking.objects.create(
        user=user,
        slot=slot,
        status=BookingStatus.PENDING,
        expires_at=now + timedelta(minutes=hold_minutes()),
    )


# ---------- cancel ----------
@transaction.atomic
def cancel_booking(user, booking_id):
    """Cancel one of MY bookings. Frees the slot for others."""
    try:
        # select_for_update locks this one booking row until the function ends, so the
        # cancel cannot clash with another request changing the same booking.
        # of=("self",) = lock only the booking row (not the joined slot and venue rows).
        booking = (
            Booking.objects.select_for_update(of=("self",))
            .select_related("slot__venue")
            .get(pk=booking_id, user=user)
        )
    except Booking.DoesNotExist:
        raise BookingNotFound("Booking not found.") from None

    if booking.status not in ACTIVE_STATUSES:
        raise BookingNotCancellable(f"A {booking.status.lower()} booking cannot be cancelled.")
    if booking.slot.start_time <= timezone.now():
        raise BookingNotCancellable("This slot has already started.")

    was_confirmed = booking.status == BookingStatus.CONFIRMED
    booking.status = BookingStatus.CANCELLED
    booking.save(update_fields=["status", "updated_at"])

    # DAY 11: a CONFIRMED booking was paid for. Cancelling it must give the money back, in the
    # SAME transaction, so "booking cancelled but money kept" can never be saved.
    # (Policy used here: full refund. A real business might refund less close to the start time.)
    if was_confirmed:
        # Imported here because payments.services itself imports from bookings (circular import).
        from payments.models import PaymentStatus
        from payments.services import request_refund

        # Lock order: booking (locked above), then payment rows. Same order as the webhook.
        paid = booking.payments.select_for_update(of=("self",)).filter(status=PaymentStatus.PAID)
        for payment in paid:
            request_refund(payment, "Booking cancelled by the user.")
    return booking
