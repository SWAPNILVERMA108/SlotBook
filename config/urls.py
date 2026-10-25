from django.conf import settings
from django.contrib import admin
from django.db import connection
from django.urls import include, path
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.routers import DefaultRouter

from bookings.views import BookingViewSet
from venues.views import SlotViewSet, VenueViewSet


def redis_status():
    """Is the Celery broker (Redis) reachable? One second timeout, so health never hangs."""
    try:
        import redis

        url = getattr(settings, "CELERY_BROKER_URL", "redis://localhost:6379/0")
        redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1).ping()
        return "connected"
    except Exception:
        return "unavailable"


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([AllowAny])  # anyone can call this one, no login needed
@throttle_classes([])  # hosting platforms call this every few seconds: never rate limit it
def health_check(request):
    """Is the app alive? The database is required. Redis is only reported."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        database = "connected"
    except Exception:
        database = "unavailable"

    body = {
        "status": "ok" if database == "connected" else "error",
        "database": database,
        "redis": redis_status(),
    }
    return Response(body, status=200 if database == "connected" else 503)


# The router builds the standard URLs (list, detail, ...) for each ViewSet.
router = DefaultRouter()
router.register("venues", VenueViewSet, basename="venue")
router.register("slots", SlotViewSet, basename="slot")
router.register("bookings", BookingViewSet, basename="booking")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", health_check, name="health-check"),
    path("api/auth/", include("users.urls")),
    path("api/payments/", include("payments.urls")),
    path("api/", include(router.urls)),
    # Automatic API documentation
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
]
