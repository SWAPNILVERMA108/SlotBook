from rest_framework import serializers

from .models import Slot, Venue


class VenueSerializer(serializers.ModelSerializer):
    class Meta:
        model = Venue
        fields = ["id", "name", "address", "city"]
        read_only_fields = fields


class SlotSerializer(serializers.ModelSerializer):
    venue = VenueSerializer(read_only=True)

    class Meta:
        model = Slot
        fields = ["id", "venue", "start_time", "end_time", "price"]
        read_only_fields = fields
