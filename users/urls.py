from django.urls import path

from .views import LoginView, MeView, RefreshView, RegisterView

urlpatterns = [
    path("register/", RegisterView.as_view(), name="auth-register"),
    # Login: send username + password, get back an access token and a refresh token.
    path("login/", LoginView.as_view(), name="auth-login"),
    # Send the refresh token, get a new access token.
    path("refresh/", RefreshView.as_view(), name="auth-refresh"),
    path("me/", MeView.as_view(), name="auth-me"),
]
