"""Background tasks for bookings.

Run a worker (and Celery Beat, which sends the scheduled tasks):
    celery -A config worker -l info --pool=solo       (Windows)
    celery -A config worker -l info                   (Mac / Linux)
    celery -A config beat -l info                     (second terminal, both systems)
"""

import logging

from celery import shared_task
from django.db import OperationalError
from django.utils import timezone

from .models import Booking, BookingStatus

logger = logging.getLogger(__name__)


@shared_task
def ping():
    """The simplest possible task. If this returns "pong", Django, Redis and the worker all work."""
    return "pong"


@shared_task
def report_pending_bookings():
    """Counts PENDING bookings, and how many of them are already past their hold time.

    It only READS data (changes nothing).
    """
    pending = Booking.objects.filter(status=BookingStatus.PENDING)
    return {
        "pending": pending.count(),
        "overdue": pending.filter(expires_at__lte=timezone.now()).count(),
    }


# ---------- DAY 9 ----------
@shared_task(autoretry_for=(OperationalError,), retry_backoff=5, max_retries=3)
def release_expired_bookings():
    """Frees every slot whose 10-minute hold has ended. Runs every minute (Celery Beat).

    WHY ONE .update()?
      - It is ONE SQL statement, so PostgreSQL does the whole job atomically.
      - It is IDEMPOTENT: running it twice in a row gives the same final state. The second run
        finds nothing left to expire and returns 0.
      - If a payment is being confirmed at the same moment, PostgreSQL makes this UPDATE wait for
        that booking row and then re-checks the WHERE condition. A booking that just became
        CONFIRMED no longer matches `status=PENDING`, so it is left alone.

    autoretry_for=(OperationalError,): in the very rare case PostgreSQL detects a deadlock and
    cancels this statement, Celery simply tries again a few seconds later.

    Returns how many bookings were expired, so the worker log shows what happened.
    """
    now = timezone.now()
    expired = Booking.objects.filter(
        status=BookingStatus.PENDING,
        expires_at__lte=now,
    ).update(
        status=BookingStatus.EXPIRED,
        updated_at=now,  # .update() skips auto_now, so we set it ourselves
    )
    if expired:
        logger.info("Released %s expired booking hold(s)", expired)
    return expired
