from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class Venue(models.Model):
    """A place that owns bookable slots, for example a turf, a gym or a clinic."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,  # never silently delete a user who owns venues
        related_name="venues",
    )
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=80, db_index=True)  # customers filter by city
    is_active = models.BooleanField(default=True)  # hide a venue without deleting it
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.city})"


class Slot(models.Model):
    """One bookable time window at a venue, for example 6 PM to 7 PM on Saturday."""

    venue = models.ForeignKey(
        Venue,
        on_delete=models.PROTECT,  # a venue with slot history cannot be deleted by mistake
        related_name="slots",
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    # DecimalField is exact. FloatField would store 0.1 + 0.2 as 0.30000000000000004.
    price = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )
    is_active = models.BooleanField(default=True)  # hide a slot without deleting it
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_time"]
        constraints = [
            # The end must come after the start.
            models.CheckConstraint(
                condition=Q(end_time__gt=F("start_time")),
                name="slot_end_after_start",
            ),
            # A price can be 0 (free slot) but never negative.
            models.CheckConstraint(
                condition=Q(price__gte=0),
                name="slot_price_not_negative",
            ),
            # The same venue cannot have two slots that start at the same moment.
            models.UniqueConstraint(
                fields=["venue", "start_time"],
                name="unique_slot_start_per_venue",
            ),
        ]
        indexes = [
            # The slot list is mostly filtered by date and active flag.
            models.Index(fields=["is_active", "start_time"], name="slot_active_start_idx"),
        ]

    def __str__(self):
        # start_time comes from the database in UTC. localtime() converts it to our
        # TIME_ZONE (Asia/Kolkata), so the admin shows the time people actually expect.
        return f"{self.venue.name} | {timezone.localtime(self.start_time):%d %b %Y %H:%M}"
