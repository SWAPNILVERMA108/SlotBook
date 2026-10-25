import logging

from django.db import IntegrityError
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from config.pagination import StandardPagination

from . import services
from .models import Booking
from .serializers import BookingCreateSerializer, BookingSerializer

logger = logging.getLogger(__name__)


class BookingViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    GET  /api/bookings/            my bookings (filter: ?status=PENDING)
    GET  /api/bookings/me/         same list (alias)
    GET  /api/bookings/<id>/       one of my bookings
    POST /api/bookings/            book a slot      body: {"slot_id": 1}
    POST /api/bookings/<id>/cancel/
    """

    serializer_class = BookingSerializer
    pagination_class = StandardPagination
    filterset_fields = ["status"]
    # Only digits are allowed in /api/bookings/<id>/. Without this, /api/bookings/abc/cancel/
    # would reach our service code and crash with a 500 error.
    lookup_value_regex = r"\d+"

    def get_queryset(self):
        # drf-spectacular builds the API docs WITHOUT a real request or user. Without this
        # guard, "self.request.user" crashed there and the docs showed a warning.
        if getattr(self, "swagger_fake_view", False):
            return Booking.objects.none()
        # A user can only ever see THEIR OWN bookings. Asking for someone else's gives 404.
        return Booking.objects.filter(user=self.request.user).select_related("slot__venue")

    @extend_schema(request=BookingCreateSerializer, responses={201: BookingSerializer})
    def create(self, request):
        serializer = BookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            booking = services.create_booking(request.user, serializer.validated_data["slot_id"])
        except services.SlotNotFound as error:
            return Response({"detail": str(error)}, status=status.HTTP_404_NOT_FOUND)
        except services.SlotUnavailable:
            return Response(
                {"detail": "This slot has just been booked by someone else."},
                status=status.HTTP_409_CONFLICT,
            )
        except IntegrityError:
            # With the row lock in create_booking() this should NEVER happen. If it does, the
            # database constraint still stopped a double booking (good), but our locking logic
            # has a hole (bad), so we write a loud log line to find it.
            logger.error(
                "IntegrityError while booking slot %s: the lock did not prevent a clash",
                serializer.validated_data["slot_id"],
                exc_info=True,
            )
            return Response(
                {"detail": "This slot has just been booked by someone else."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(BookingSerializer(booking).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={200: BookingSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        try:
            booking = services.cancel_booking(request.user, pk)
        except services.BookingNotFound as error:
            return Response({"detail": str(error)}, status=status.HTTP_404_NOT_FOUND)
        except services.BookingNotCancellable as error:
            return Response({"detail": str(error)}, status=status.HTTP_409_CONFLICT)
        return Response(BookingSerializer(booking).data)

    @extend_schema(responses=BookingSerializer(many=True))
    @action(detail=False, methods=["get"])
    def me(self, request):
        return self.list(request)
