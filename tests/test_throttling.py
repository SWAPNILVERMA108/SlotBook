"""Rate limiting: too many requests in a minute get "429 Too Many Requests"."""

import pytest
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from tests.factories import PASSWORD, UserFactory

# Logging in looks the user up in the database, so every test here may touch the database.
pytestmark = pytest.mark.django_db


@pytest.fixture
def limits(settings):
    """Switch rate limiting ON with tiny limits (it is OFF while tests run, so other tests stay fast)."""

    def apply(**rates):
        settings.THROTTLING_ENABLED = True
        settings.THROTTLE_RATES = {
            "anon": "1000/min",
            "user": "1000/min",
            "auth": "1000/min",
            **rates,
        }

    return apply


def wrong_login(client, ip="1.2.3.4"):
    return client.post(
        "/api/auth/login/",
        {"username": "nobody", "password": "wrong"},
        format="json",
        REMOTE_ADDR=ip,
    )


def test_the_4th_login_attempt_in_a_minute_is_blocked(api_client, limits):
    limits(auth="3/min")
    assert [wrong_login(api_client).status_code for _ in range(3)] == [401, 401, 401]
    blocked = wrong_login(api_client)
    assert blocked.status_code == 429
    assert int(blocked["Retry-After"]) > 0


def test_each_ip_address_has_its_own_counter(api_client, limits):
    limits(auth="2/min")
    assert [wrong_login(api_client, ip="10.0.0.1").status_code for _ in range(3)] == [401, 401, 429]
    assert wrong_login(api_client, ip="10.0.0.2").status_code == 401  # a different person


def test_register_and_login_share_the_strict_auth_limit(api_client, limits):
    limits(auth="2/min")
    body = {"username": "a", "email": "a@example.com", "password": PASSWORD}
    assert api_client.post("/api/auth/register/", body, format="json").status_code == 201
    same_person = "127.0.0.1"  # the address the test client registers from
    assert wrong_login(api_client, ip=same_person).status_code == 401
    assert wrong_login(api_client, ip=same_person).status_code == 429


def test_refresh_endpoint_is_limited_too(api_client, limits):
    limits(auth="1/min")
    first = api_client.post("/api/auth/refresh/", {"refresh": "junk"}, format="json")
    second = api_client.post("/api/auth/refresh/", {"refresh": "junk"}, format="json")
    assert (first.status_code, second.status_code) == (401, 429)


def test_faking_the_x_forwarded_for_header_does_not_dodge_the_limit(api_client, limits):
    """NUM_PROXIES=0 means: trust only the real connection address, never a header the client sent."""
    limits(auth="2/min")
    codes = [
        api_client.post(
            "/api/auth/login/",
            {"username": "x", "password": "y"},
            format="json",
            REMOTE_ADDR="9.9.9.9",
            HTTP_X_FORWARDED_FOR=f"1.1.1.{n}",
        ).status_code
        for n in range(3)
    ]
    assert codes == [401, 401, 429]


def test_logged_in_users_have_their_own_limit(auth_client, limits):
    limits(user="2/min")
    assert [auth_client.get("/api/auth/me/").status_code for _ in range(3)] == [200, 200, 429]


def test_two_users_do_not_share_a_counter(limits):
    limits(user="1/min")
    clients = []
    for _ in range(2):
        client = APIClient()
        client.force_authenticate(UserFactory())
        clients.append(client)
    assert [c.get("/api/auth/me/").status_code for c in clients] == [200, 200]


def test_not_logged_in_visitors_of_open_pages_are_limited_per_ip(api_client, limits):
    limits(anon="2/min")
    codes = [api_client.get("/api/schema/").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_the_health_check_is_never_limited(api_client, limits):
    limits(anon="1/min", user="1/min")
    assert {api_client.get("/api/health/").status_code for _ in range(5)} <= {200, 503}


def test_a_broken_cache_does_not_take_the_api_down(api_client, limits, monkeypatch):
    """Fail open: if Redis is down, rate limiting is skipped instead of every request failing."""

    class BrokenCache:
        def get(self, *args, **kwargs):
            raise ConnectionError("redis is down")

        set = get

    limits(auth="1/min")
    monkeypatch.setattr(SimpleRateThrottle, "cache", BrokenCache())
    codes = [wrong_login(api_client).status_code for _ in range(3)]
    assert codes == [401, 401, 401]  # no 500, and no 429 either


def test_limits_are_off_by_default_while_tests_run(api_client):
    assert [wrong_login(api_client).status_code for _ in range(15)] == [401] * 15
