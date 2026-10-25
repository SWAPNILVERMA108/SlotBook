"""Rate limiting: how many requests one address or user may send per minute.

It stops password guessing on /login/, and stops one client from flooding the API.
The limits live in settings.THROTTLE_RATES (change them with environment variables).
"""

import logging

from django.conf import settings
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

logger = logging.getLogger(__name__)


class _SettingsRateMixin:
    """Two small improvements over DRF's own throttles."""

    def get_rate(self):
        # Read at request time, so changing the settings (for example in a test) works.
        return settings.THROTTLE_RATES.get(self.scope)

    def allow_request(self, request, view):
        if not settings.THROTTLING_ENABLED:
            return True
        try:
            return super().allow_request(request, view)
        except Exception:
            # FAIL OPEN: if the cache (Redis) is broken, let the request through. A broken
            # rate limiter must not take the whole API down.
            logger.exception("Rate limit check failed; letting the request through")
            return True


class AnonThrottle(_SettingsRateMixin, AnonRateThrottle):
    """People who are not logged in, counted per IP address."""

    scope = "anon"


class UserThrottle(_SettingsRateMixin, UserRateThrottle):
    """Logged-in users, counted per user."""

    scope = "user"


class AuthThrottle(_SettingsRateMixin, AnonRateThrottle):
    """Register, login and token refresh: strict, per IP address. Slows down password guessing."""

    scope = "auth"
