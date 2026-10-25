from rest_framework import serializers

from venues.serializers import SlotSerializer

from .models import Booking


class BookingSerializer(serializers.ModelSerializer):
    """How a booking looks in API answers (read only)."""

    slot = SlotSerializer(read_only=True)

    class Meta:
        model = Booking
        fields = ["id", "slot", "status", "expires_at", "created_at"]
        read_only_fields = fields


class BookingCreateSerializer(serializers.Serializer):
    """What the client must send to book: just the slot id."""

    slot_id = serializers.IntegerField(min_value=1)
