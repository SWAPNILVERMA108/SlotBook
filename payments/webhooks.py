"""Handling of Razorpay webhook events (the logic behind the webhook URL).

Razorpay can deliver the SAME event more than once (retries after a timeout, network trouble).
We must therefore be able to receive an event twice and still do the work only once.
"""

import logging

from django.db import IntegrityError, transaction

from .models import WebhookEvent
from .services import PaymentNotFound, mark_payment_failed, settle_successful_payment

logger = logging.getLogger(__name__)

SUCCESS_EVENTS = {"payment.captured", "order.paid"}
FAILURE_EVENTS = {"payment.failed"}


def _payment_entity(payload):
    entity = ((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
    return entity if isinstance(entity, dict) else {}


def _dispatch(event_type, payload):
    entity = _payment_entity(payload)
    order_id = entity.get("order_id")
    payment_id = entity.get("id")

    if event_type in SUCCESS_EVENTS:
        if not order_id or not payment_id:
            return "ignored_malformed"
        try:
            settlement = settle_successful_payment(
                order_id, payment_id, amount_paise=entity.get("amount")
            )
        except PaymentNotFound:
            # Not our order (or a Razorpay test ping). Answer 200 so Razorpay stops retrying.
            return "ignored_unknown_order"
        return settlement.outcome

    if event_type in FAILURE_EVENTS:
        if not order_id:
            return "ignored_malformed"
        noted = mark_payment_failed(order_id, entity.get("error_description") or "")
        return "payment_failed_recorded" if noted else "ignored"

    return "ignored_event"


def handle_event(event_id, event_type, payload):
    """Process one webhook delivery exactly once. Returns a short result text.

    The whole thing is ONE transaction:
      1. insert the event id          (UNIQUE -> a duplicate delivery fails right here)
      2. do the real work             (settle the payment)
      3. commit
    If step 2 crashes, step 1 is rolled back too. Then the event is NOT recorded, we answer
    500, and Razorpay's retry gets processed normally. If two copies arrive at the same time,
    the second INSERT waits for the first to finish, then fails, and is answered as duplicate.
    """
    with transaction.atomic():
        try:
            with (
                transaction.atomic()
            ):  # a savepoint, so a failed insert does not poison the outer one
                event = WebhookEvent.objects.create(event_id=event_id, event_type=event_type[:64])
        except IntegrityError:
            logger.info("Duplicate webhook %s ignored", event_id)
            return "duplicate_ignored"

        result = _dispatch(event_type, payload)
        event.result = result[:255]
        event.save(update_fields=["result"])
        logger.info("Webhook %s (%s) -> %s", event_id, event_type, result)
        return result
