from django.db import models
from django.db.models import Q


class PaymentStatus(models.TextChoices):
    CREATED = "CREATED", "Order created"
    PAID = "PAID", "Paid"
    FAILED = "FAILED", "Failed"
    REFUND_PENDING = "REFUND_PENDING", "Refund pending"
    REFUNDED = "REFUNDED", "Refunded"


class Payment(models.Model):
    """One Razorpay order for a booking. A booking may have several (e.g. after an expiry)."""

    booking = models.ForeignKey(
        "bookings.Booking",
        on_delete=models.PROTECT,  # financial records are never deleted with a booking
        related_name="payments",
    )
    # Razorpay's order id. Unique, so the same order can never be saved twice.
    razorpay_order_id = models.CharField(max_length=64, unique=True)
    # Empty until the user actually pays. null=True (not "") so that many unpaid rows can
    # exist while real payment ids stay unique. PostgreSQL allows many NULLs in a unique column.
    razorpay_payment_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    # Set when we refund (Day 11). Also nullable + unique for the same reason as above.
    razorpay_refund_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)  # in rupees
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.CREATED,
    )
    # A short human explanation, e.g. "Hold expired and someone else took the slot".
    status_note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_positive"),
        ]
        indexes = [
            models.Index(fields=["booking", "status"], name="payment_booking_status_idx"),
            # The refund sweeper looks for REFUND_PENDING rows.
            models.Index(fields=["status", "updated_at"], name="payment_status_updated_idx"),
        ]

    def __str__(self):
        return f"Payment {self.razorpay_order_id} | {self.status}"


class WebhookEvent(models.Model):
    """One row for every webhook delivery we have fully handled (Day 11).

    The UNIQUE event_id is the whole trick: Razorpay may deliver the same event twice, and the
    second INSERT fails, so the second delivery is recognised and ignored. We store only
    non-sensitive facts (no card data, no customer details).
    """

    event_id = models.CharField(max_length=128, unique=True)
    event_type = models.CharField(max_length=64)
    result = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event_type} | {self.event_id}"
