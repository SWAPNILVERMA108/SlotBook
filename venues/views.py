from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets
from rest_framework.filters import OrderingFilter

from config.pagination import StandardPagination

from .filters import SlotFilter
from .models import Venue
from .selectors import available_slots
from .serializers import SlotSerializer, VenueSerializer


class VenueViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/venues/ and /api/venues/<id>/ (venues are created in the admin for now)."""

    queryset = Venue.objects.filter(is_active=True)
    serializer_class = VenueSerializer
    pagination_class = StandardPagination
    filterset_fields = ["city"]


class SlotViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/slots/ and /api/slots/<id>/ - only slots that can still be booked."""

    serializer_class = SlotSerializer
    pagination_class = StandardPagination
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = SlotFilter
    ordering_fields = ["start_time", "price"]
    ordering = ["start_time"]

    def get_queryset(self):
        return available_slots()
