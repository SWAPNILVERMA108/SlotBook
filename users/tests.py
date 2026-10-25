from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

User = get_user_model()

GOOD_PASSWORD = "Sl0t-Book!2026"


class AuthApiTests(APITestCase):
    def register(self, **overrides):
        data = {
            "username": "alice",
            "email": "alice@example.com",
            "phone": "9999999999",
            "password": GOOD_PASSWORD,
        }
        data.update(overrides)
        return self.client.post(reverse("auth-register"), data, format="json")

    def login(self, username="alice", password=GOOD_PASSWORD):
        return self.client.post(
            reverse("auth-login"), {"username": username, "password": password}, format="json"
        )

    def test_register_creates_user_and_hides_password(self):
        response = self.register()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("password", response.data)
        user = User.objects.get(username="alice")
        self.assertTrue(user.check_password(GOOD_PASSWORD))  # stored hashed, not as plain text
        self.assertNotEqual(user.password, GOOD_PASSWORD)

    def test_register_rejects_duplicate_email_ignoring_case(self):
        self.register()
        response = self.register(username="alice2", email="ALICE@example.com")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)

    def test_register_rejects_weak_password(self):
        response = self.register(password="12345678")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.data)

    def test_register_requires_email(self):
        response = self.register(email="")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_login_returns_access_and_refresh_tokens(self):
        self.register()
        response = self.login()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_login_with_wrong_password_is_401(self):
        self.register()
        response = self.login(password="wrong-password")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_without_token_is_401(self):
        response = self.client.get(reverse("auth-me"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_with_token_returns_my_profile(self):
        self.register()
        access = self.login().data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        response = self.client.get(reverse("auth-me"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["username"], "alice")
        self.assertNotIn("password", response.data)

    def test_refresh_gives_a_new_access_token(self):
        self.register()
        refresh = self.login().data["refresh"]
        response = self.client.post(reverse("auth-refresh"), {"refresh": refresh}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)

    def test_garbage_token_is_rejected(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")
        response = self.client.get(reverse("auth-me"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
