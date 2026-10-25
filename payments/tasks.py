"""Background tasks for payments."""

from celery import shared_task

from .models import Payment, PaymentStatus
from .services import RefundFailed, refund_payment


@shared_task(
    autoretry_for=(RefundFailed,),
    retry_backoff=30,  # wait 30s, then 60s, then 120s ... between tries
    retry_backoff_max=900,  # but never wait more than 15 minutes
    max_retries=5,
)
def process_refund(payment_pk):
    """Refund ONE payment. Safe to run twice: refund_payment() skips anything not REFUND_PENDING."""
    return refund_payment(payment_pk)


@shared_task
def process_pending_refunds():
    """The safety net, scheduled every 5 minutes by Celery Beat.

    Normally a refund is queued the moment a payment becomes REFUND_PENDING. If that message
    was lost (Redis was down, the worker crashed), this sweep finds the payment and queues it
    again. Nothing can stay unrefunded for long.
    """
    pks = list(
        Payment.objects.filter(status=PaymentStatus.REFUND_PENDING).values_list("pk", flat=True)
    )
    for pk in pks:
        process_refund.delay(pk)
    return len(pks)
