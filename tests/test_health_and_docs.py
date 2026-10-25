"""The health check, the API documentation and the admin must always work."""

from unittest import mock

import pytest
from django.core.management import call_command
from django.db import OperationalError, connection


def test_health_ok_when_database_and_redis_answer(api_client, db):
    with mock.patch("config.urls.redis_status", return_value="connected"):
        response = api_client.get("/api/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected", "redis": "connected"}


def test_health_still_200_when_only_redis_is_down(api_client, db):
    """Redis is reported, but the web part of the app does not need it to stay alive."""
    with mock.patch("config.urls.redis_status", return_value="unavailable"):
        response = api_client.get("/api/health/")
    assert response.status_code == 200
    assert response.json()["redis"] == "unavailable"


def test_health_is_503_when_the_database_is_down(api_client, db):
    with mock.patch("config.urls.redis_status", return_value="connected"):
        with mock.patch.object(connection, "cursor", side_effect=OperationalError("db down")):
            response = api_client.get("/api/health/")
    assert response.status_code == 503
    assert response.json()["status"] == "error"
    assert response.json()["database"] == "unavailable"


def test_redis_status_reports_unavailable_instead_of_crashing(settings):
    from config.urls import redis_status

    settings.CELERY_BROKER_URL = "redis://127.0.0.1:1/0"  # nothing listens on port 1
    assert redis_status() == "unavailable"


def test_the_api_documentation_builds_without_any_warning(tmp_path):
    """Swagger is part of the project's front door. Warnings there mean wrong or missing docs."""
    call_command(
        "spectacular", "--validate", "--fail-on-warn", "--file", str(tmp_path / "schema.yml")
    )
    assert (tmp_path / "schema.yml").stat().st_size > 1000


def test_documentation_lists_every_endpoint(api_client):
    paths = api_client.get("/api/schema/", {"format": "json"}).json()["paths"]
    for expected in (
        "/api/auth/register/",
        "/api/auth/login/",
        "/api/auth/refresh/",
        "/api/auth/me/",
        "/api/slots/",
        "/api/venues/",
        "/api/bookings/",
        "/api/bookings/{id}/cancel/",
        "/api/payments/create-order/",
        "/api/payments/verify/",
        "/api/health/",
    ):
        assert expected in paths, expected


def test_swagger_page_and_admin_login_page_load(api_client, db):
    assert api_client.get("/api/docs/").status_code == 200
    assert api_client.get("/admin/login/").status_code == 200


def test_the_demo_payment_page_is_hidden_unless_debug_is_on(api_client, settings):
    settings.DEBUG = False
    assert api_client.get("/api/payments/demo/").status_code == 404
    settings.DEBUG = True
    response = api_client.get("/api/payments/demo/")
    assert response.status_code == 200
    assert b"checkout.razorpay.com" in response.content


def test_unknown_urls_give_404_not_500(api_client):
    assert api_client.get("/api/nothing-here/").status_code == 404


@pytest.mark.django_db
def test_every_model_has_a_readable_name():
    from payments.models import WebhookEvent
    from tests.factories import (
        BookingFactory,
        PaymentFactory,
        SlotFactory,
        UserFactory,
        VenueFactory,
    )

    booking = BookingFactory()
    payment = PaymentFactory(booking=booking)
    texts = [
        str(UserFactory(username="zed")),
        str(VenueFactory(name="Turf A", city="Noida")),
        str(SlotFactory()),
        str(booking),
        str(payment),
        str(WebhookEvent(event_id="evt_1", event_type="payment.captured")),
    ]
    assert texts[0] == "zed"
    assert texts[1] == "Turf A (Noida)"
    assert all(text and "object (" not in text for text in texts)
