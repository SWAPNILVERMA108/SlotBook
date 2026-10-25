"""Payment business rules. Views and webhooks only call the functions in this file.

THE BIG IDEA (read this first)
------------------------------
Money and bookings can get out of step in three ways:
  1. The user pays, but we never hear about it.
       -> We have TWO ways to hear: the checkout callback (verify) and the webhook.
  2. We hear about the same payment twice.
       -> settle_successful_payment() is IDEMPOTENT: the second call changes nothing.
  3. The user pays AFTER the 10-minute hold ended.
       -> settle_successful_payment() decides: re-confirm the booking, or refund.

Rules we never break:  never a double booking, and never keep a user's money without a booking.

LOCK ORDER (same rule as bookings/services.py): SLOT row first, then BOOKING row, then PAYMENT row.
Code that locks the same rows in different orders can deadlock.
"""

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

try:
    import razorpay
except ImportError as error:  # pragma: no cover  (only happens on a machine missing the package)
    raise ImportError(
        "The 'razorpay' package is not installed in this Python environment.\n"
        "Fix:  python -m pip install -r requirements.txt   (or:  python -m pip install razorpay)"
    ) from error
import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from razorpay.errors import SignatureVerificationError
from razorpay.utility import Utility

from bookings.models import Booking, BookingStatus
from bookings.services import BookingNotFound  # noqa: F401  (re-exported for the views)
from venues.models import Slot

from .models import Payment, PaymentStatus

logger = logging.getLogger(__name__)

RAZORPAY_TIMEOUT_SECONDS = 10

# What settle_successful_payment() can report:
OUTCOME_CONFIRMED = "confirmed"
OUTCOME_RECONFIRMED = "confirmed_after_hold_expired"
OUTCOME_REFUND = "refund_pending"
OUTCOME_DUPLICATE = "already_processed"
OUTCOME_AMOUNT_MISMATCH = "amount_mismatch"


# ---------- our own error types ----------
class PaymentError(Exception):
    """Base class for expected payment problems."""


class PaymentConfigError(PaymentError):
    """Razorpay keys are missing in settings / .env."""


class BookingNotPayable(PaymentError):
    """The booking is not PENDING, or its hold has ended."""


class PaymentNotFound(PaymentError):
    """We have no payment with this Razorpay order id."""


class RazorpayUnavailable(PaymentError):
    """Razorpay did not answer properly (network error, timeout, or an error answer)."""


class InvalidSignature(PaymentError):
    """The signature does not match: the message is fake or was changed on the way."""


class RefundFailed(PaymentError):
    """Razorpay refused or could not do the refund right now. The task will retry."""


# ---------- talking to Razorpay ----------
class _TimeoutSession(requests.Session):
    """A normal requests session that ALWAYS has a timeout.

    The Razorpay library does not set one. Without a timeout, a slow Razorpay would freeze our
    web worker forever. Every call now gives up after RAZORPAY_TIMEOUT_SECONDS.
    """

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", RAZORPAY_TIMEOUT_SECONDS)
        return super().request(method, url, **kwargs)


def make_session():
    """A separate function so tests can swap in a fake Razorpay server."""
    return _TimeoutSession()


def get_client():
    key_id = getattr(settings, "RAZORPAY_KEY_ID", "")
    key_secret = getattr(settings, "RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        raise PaymentConfigError("Razorpay keys are not configured.")
    return razorpay.Client(auth=(key_id, key_secret), session=make_session())


def rupees_to_paise(amount):
    """Razorpay wants the smallest unit: paise. 500.00 rupees -> 50000.

    We use Decimal math, never float: 19.99 * 100 in float is 1998.9999999999998.
    """
    return int((Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ---------- DAY 10: create an order ----------
def _order_payload(booking, payment):
    return {
        "booking_id": booking.id,
        "order_id": payment.razorpay_order_id,
        "amount": rupees_to_paise(payment.amount),  # paise, exactly what Razorpay Checkout needs
        "currency": payment.currency,
        "key_id": settings.RAZORPAY_KEY_ID,  # the PUBLIC key id (never the secret!)
        "expires_at": booking.expires_at,
        "free": False,
    }


def create_order_for_booking(user, booking_id):
    """Creates (or re-uses) the Razorpay order for one of MY pending bookings.

    Returns a dict the frontend gives to Razorpay Checkout.
    """
    with transaction.atomic():
        # Lock this booking row. Cancel and "create order" for the SAME booking now take turns.
        try:
            booking = (
                Booking.objects.select_for_update(of=("self",))
                .select_related("slot")
                .get(pk=booking_id, user=user)
            )
        except Booking.DoesNotExist:
            raise BookingNotFound("Booking not found.") from None

        now = timezone.now()
        if booking.status != BookingStatus.PENDING:
            raise BookingNotPayable(f"A {booking.status.lower()} booking cannot be paid.")
        if booking.expires_at is None or booking.expires_at <= now:
            raise BookingNotPayable("The hold on this slot has ended. Please book the slot again.")

        amount = booking.slot.price

        # A FREE slot needs no payment (Razorpay cannot create an order for 0). Without this,
        # a free booking could never leave PENDING.
        if rupees_to_paise(amount) == 0:
            booking.status = BookingStatus.CONFIRMED
            booking.save(update_fields=["status", "updated_at"])
            return {"booking_id": booking.id, "free": True, "booking_status": booking.status}

        # Double click or page refresh? Re-use the open order instead of creating many.
        existing = (
            booking.payments.filter(status=PaymentStatus.CREATED, amount=amount)
            .order_by("-created_at")
            .first()
        )
        if existing:
            return _order_payload(booking, existing)

        client = get_client()
        try:
            order = client.order.create(
                {
                    "amount": rupees_to_paise(amount),
                    "currency": "INR",
                    "receipt": f"booking_{booking.id}",  # Razorpay allows up to 40 characters
                    "notes": {"booking_id": str(booking.id), "slot_id": str(booking.slot_id)},
                }
            )
            order_id = order["id"]
        except Exception as error:
            logger.error("Razorpay order creation failed for booking %s: %r", booking.id, error)
            raise RazorpayUnavailable("Could not create the payment order. Try again.") from error

        payment = Payment.objects.create(
            booking=booking,
            razorpay_order_id=order_id,
            amount=amount,
            currency="INR",
        )
        return _order_payload(booking, payment)


# ---------- DAY 10: signatures ----------
def verify_checkout_signature(order_id, payment_id, signature):
    """After checkout, Razorpay gives the browser a signature = HMAC-SHA256("order|payment")
    signed with OUR secret key. Only Razorpay and we know that key, so a valid signature proves
    that Razorpay really said "this payment belongs to this order".

    NEVER trust the browser's own claim "I paid". Always check this signature on the server.
    """
    client = get_client()
    try:
        client.utility.verify_payment_signature(
            {
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature,
            }
        )
    except (SignatureVerificationError, TypeError, ValueError):
        # TypeError/ValueError: odd characters in a fake signature must give a clean 400,
        # not a crash (hmac.compare_digest refuses non-ASCII text).
        raise InvalidSignature("Payment signature is invalid.") from None


def verify_webhook_signature(raw_body, signature):
    """Webhooks are signed with a DIFFERENT secret (RAZORPAY_WEBHOOK_SECRET), over the RAW body.

    "Raw" means the exact bytes Razorpay sent. If you parse the JSON and dump it again, spaces
    and key order change, and the signature no longer matches.
    """
    secret = getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        raise PaymentConfigError("Webhook secret is not configured.")
    try:
        Utility().verify_webhook_signature(raw_body.decode("utf-8"), signature, secret)
    except (SignatureVerificationError, TypeError, ValueError, UnicodeDecodeError):
        raise InvalidSignature("Webhook signature is invalid.") from None


# ---------- DAY 11: the heart. A successful payment arrives. ----------
@dataclass
class Settlement:
    outcome: str
    payment: Payment
    booking: Booking


def _queue_refund(payment_pk):
    from .tasks import process_refund  # imported here to avoid a circular import

    process_refund.delay(payment_pk)


def request_refund(payment, reason):
    """Marks a payment REFUND_PENDING and queues the refund. Call it with the payment row locked,
    inside a transaction. Used when a late/duplicate payment cannot be honoured AND when a user
    cancels a booking they already paid for.
    """
    payment.status = PaymentStatus.REFUND_PENDING
    payment.status_note = reason[:255]
    payment.save(update_fields=["razorpay_payment_id", "status", "status_note", "updated_at"])
    logger.warning("Payment %s needs a refund: %s", payment.razorpay_order_id, reason)
    # Queue the refund only AFTER the database commit (a task must never see uncommitted
    # data). robust=True: if the queue (Redis) is down, only log it and do NOT crash. The
    # "process_pending_refunds" task (every 5 minutes) will pick this payment up anyway.
    transaction.on_commit(lambda: _queue_refund(payment.pk), robust=True)


def _needs_refund(payment, booking, razorpay_payment_id, reason):
    payment.razorpay_payment_id = razorpay_payment_id
    request_refund(payment, reason)
    return Settlement(OUTCOME_REFUND, payment, booking)


@transaction.atomic
def settle_successful_payment(razorpay_order_id, razorpay_payment_id, amount_paise=None):
    """The user's money has arrived. Decide what happens to the booking. IDEMPOTENT.

    Called from two places (whichever comes first wins, the other becomes a harmless no-op):
      - the verify endpoint (browser callback after checkout)
      - the webhook (Razorpay tells our server directly)

    Decision table:
      booking PENDING (hold live, or ended but Celery has not expired it yet)  -> CONFIRM
      booking EXPIRED, slot still free and bookable                            -> CONFIRM AGAIN
      booking EXPIRED, someone else holds the slot / slot is gone              -> REFUND
      booking CANCELLED                                                        -> REFUND
      booking already CONFIRMED (this is a second payment)                     -> REFUND
    """
    # Find the payment first (no lock yet) only to learn which slot to lock first.
    try:
        found = Payment.objects.select_related("booking").get(razorpay_order_id=razorpay_order_id)
    except Payment.DoesNotExist:
        raise PaymentNotFound(f"Unknown order {razorpay_order_id}") from None

    # LOCK ORDER: slot, then booking, then payment.
    slot = (
        Slot.objects.select_for_update(of=("self",))
        .select_related("venue")
        .get(pk=found.booking.slot_id)
    )
    booking = Booking.objects.select_for_update(of=("self",)).get(pk=found.booking_id)
    payment = Payment.objects.select_for_update(of=("self",)).get(pk=found.pk)

    # IDEMPOTENCY: the second delivery of the same news changes nothing.
    # (FAILED is allowed through: one Razorpay order can have a failed try and then a good one.)
    if payment.status in (PaymentStatus.PAID, PaymentStatus.REFUND_PENDING, PaymentStatus.REFUNDED):
        return Settlement(OUTCOME_DUPLICATE, payment, booking)

    # Safety check: the amount Razorpay reports must be the amount we asked for.
    expected = rupees_to_paise(payment.amount)
    if amount_paise is not None and int(amount_paise) != expected:
        payment.status_note = f"AMOUNT MISMATCH: paid {amount_paise}, expected {expected}. Review!"[
            :255
        ]
        payment.razorpay_payment_id = razorpay_payment_id
        payment.save(update_fields=["razorpay_payment_id", "status_note", "updated_at"])
        logger.error(
            "Amount mismatch on %s: paid %s, expected %s", razorpay_order_id, amount_paise, expected
        )
        return Settlement(OUTCOME_AMOUNT_MISMATCH, payment, booking)

    now = timezone.now()

    # CASE 1: still PENDING. Even if the 10 minutes just ended, nobody took the slot, because
    # create_booking() would have marked this booking EXPIRED first. Honour the payment.
    if booking.status == BookingStatus.PENDING:
        booking.status = BookingStatus.CONFIRMED
        booking.save(update_fields=["status", "updated_at"])
        payment.razorpay_payment_id = razorpay_payment_id
        payment.status = PaymentStatus.PAID
        payment.status_note = ""
        payment.save(update_fields=["razorpay_payment_id", "status", "status_note", "updated_at"])
        return Settlement(OUTCOME_CONFIRMED, payment, booking)

    # CASE 2: the hold expired before the money arrived.
    if booking.status == BookingStatus.EXPIRED:
        slot_still_bookable = slot.is_active and slot.venue.is_active and slot.start_time > now
        if not slot_still_bookable:
            return _needs_refund(
                payment,
                booking,
                razorpay_payment_id,
                "Hold expired and the slot is no longer available.",
            )

        someone_else_holds = (
            Booking.objects.filter(slot=slot)
            .exclude(pk=booking.pk)
            .filter(
                Q(status=BookingStatus.CONFIRMED)
                | Q(status=BookingStatus.PENDING, expires_at__gt=now)
            )
            .exists()
        )
        if someone_else_holds:
            return _needs_refund(
                payment,
                booking,
                razorpay_payment_id,
                "Hold expired and someone else took the slot.",
            )

        # The slot is free. First expire stale PENDING rows of other users (their hold ended
        # but Celery has not cleaned them yet). Otherwise the database rule "one active
        # booking per slot" would reject our CONFIRMED row.
        Booking.objects.filter(slot=slot, status=BookingStatus.PENDING).exclude(
            pk=booking.pk
        ).update(status=BookingStatus.EXPIRED, updated_at=now)
        booking.status = BookingStatus.CONFIRMED
        booking.save(update_fields=["status", "updated_at"])
        payment.razorpay_payment_id = razorpay_payment_id
        payment.status = PaymentStatus.PAID
        payment.status_note = "Paid after the hold expired; slot was still free."
        payment.save(update_fields=["razorpay_payment_id", "status", "status_note", "updated_at"])
        logger.info(
            "Booking %s re-confirmed after expiry (payment %s)", booking.pk, razorpay_order_id
        )
        return Settlement(OUTCOME_RECONFIRMED, payment, booking)

    # CASE 3 and 4: CANCELLED, or already CONFIRMED by another payment.
    if booking.status == BookingStatus.CANCELLED:
        reason = "Booking was cancelled before the payment arrived."
    else:
        reason = "Booking was already confirmed by another payment (duplicate payment)."
    return _needs_refund(payment, booking, razorpay_payment_id, reason)


def mark_payment_failed(razorpay_order_id, reason=""):
    """A try failed (wrong OTP, bank declined...). The user may try again on the same order,
    so we only note it. The booking is NOT touched. Returns True if a row was updated."""
    updated = Payment.objects.filter(
        razorpay_order_id=razorpay_order_id, status=PaymentStatus.CREATED
    ).update(
        status=PaymentStatus.FAILED,
        status_note=(reason or "Payment attempt failed")[:255],
        updated_at=timezone.now(),
    )
    return bool(updated)


# ---------- DAY 11: refunds ----------
def refund_payment(payment_pk):
    """Sends the refund to Razorpay for ONE payment that is REFUND_PENDING. IDEMPOTENT.

    The payment row stays locked during the API call, so two workers can never refund the
    same payment twice: the second one waits, then sees REFUNDED and does nothing.
    """
    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update(of=("self",)).get(pk=payment_pk)
        except Payment.DoesNotExist:
            return "missing"
        if payment.status != PaymentStatus.REFUND_PENDING:
            return "skipped"
        if not payment.razorpay_payment_id:
            logger.error("Cannot refund payment %s: no Razorpay payment id stored", payment.pk)
            return "no_payment_id"

        client = get_client()
        try:
            refund = client.payment.refund(
                payment.razorpay_payment_id,
                {
                    "amount": rupees_to_paise(payment.amount),
                    "speed": "normal",
                    "notes": {"payment_pk": str(payment.pk)},
                },
            )
            refund_id = refund["id"]
        except Exception as error:
            logger.error("Refund failed for payment %s: %r", payment.pk, error)
            raise RefundFailed(f"Refund failed for payment {payment.pk}") from error

        payment.status = PaymentStatus.REFUNDED
        payment.razorpay_refund_id = refund_id
        payment.save(update_fields=["status", "razorpay_refund_id", "updated_at"])
        logger.info("Refunded payment %s (refund %s)", payment.pk, refund_id)
        return "refunded"
