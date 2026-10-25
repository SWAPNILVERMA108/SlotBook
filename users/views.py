from rest_framework import generics
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from config.throttling import AuthThrottle

from .serializers import RegisterSerializer, UserSerializer


class RegisterView(generics.CreateAPIView):
    """POST /api/auth/register/ - create a new account."""

    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]
    # No authentication here: if a browser still sends an old, expired token, we do not
    # want sign-up to fail with 401 because of it.
    authentication_classes = []
    throttle_classes = [AuthThrottle]


class LoginView(TokenObtainPairView):
    """POST /api/auth/login/ - username + password in, access and refresh token out.

    Same as SimpleJWT's view, plus a strict rate limit against password guessing.
    """

    throttle_classes = [AuthThrottle]


class RefreshView(TokenRefreshView):
    """POST /api/auth/refresh/ - refresh token in, new access token out."""

    throttle_classes = [AuthThrottle]


class MeView(generics.RetrieveAPIView):
    """GET /api/auth/me/ - who am I? (needs a valid access token)"""

    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user
