from django.conf import settings
from django.db import models
from django.db.models import Q


class BookingStatus(models.TextChoices):
    PENDING = "PENDING", "Pending payment"
    CONFIRMED = "CONFIRMED", "Confirmed"
    CANCELLED = "CANCELLED", "Cancelled"
    EXPIRED = "EXPIRED", "Expired"


# Statuses that "use up" a slot. A cancelled or expired booking frees the slot again.
ACTIVE_STATUSES = [BookingStatus.PENDING, BookingStatus.CONFIRMED]


class Booking(models.Model):
    """A user's claim on one slot. It starts as PENDING (held for a few minutes)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="bookings",
    )
    slot = models.ForeignKey(
        "venues.Slot",
        on_delete=models.PROTECT,  # booking history must survive
        related_name="bookings",
    )
    status = models.CharField(
        max_length=10,
        choices=BookingStatus.choices,
        default=BookingStatus.PENDING,
    )
    # Only meaningful while PENDING: the moment the 10-minute hold ends.
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # THE KEY RULE: at most one active booking per slot, enforced by PostgreSQL.
            models.UniqueConstraint(
                fields=["slot"],
                condition=Q(status__in=ACTIVE_STATUSES),
                name="one_active_booking_per_slot",
            ),
            # A PENDING booking must always know when its hold ends.
            models.CheckConstraint(
                condition=~Q(status=BookingStatus.PENDING) | Q(expires_at__isnull=False),
                name="pending_booking_has_expiry",
            ),
        ]
        indexes = [
            # The Celery task "find expired PENDING bookings" uses exactly these columns.
            models.Index(fields=["status", "expires_at"], name="booking_status_expiry_idx"),
            # "My bookings" page.
            models.Index(fields=["user", "status"], name="booking_user_status_idx"),
        ]

    def __str__(self):
        return f"Booking #{self.pk} | {self.user} | {self.status}"
