"""Small tests for the last rarely-used branches, so that every line of our own code is exercised."""

import logging
from datetime import timedelta
from unittest import mock

import pytest
from django.db import IntegrityError
from django.utils import timezone

from bookings import services
from payments.tests.helpers import checkout_signature, post_webhook, webhook_signature
from tests.factories import BookingFactory, PaymentFactory, SlotFactory


def test_a_database_clash_that_slips_past_the_lock_still_gives_409_and_a_loud_log(
    auth_client, slot, caplog
):
    """With the row lock this should never happen. If it ever does, the user still gets a clean
    409 (the database constraint stopped the double booking) and we write an ERROR log line."""
    with mock.patch("bookings.views.services.create_booking", side_effect=IntegrityError("clash")):
        with caplog.at_level(logging.ERROR, logger="bookings.views"):
            response = auth_client.post("/api/bookings/", {"slot_id": slot.id}, format="json")
    assert response.status_code == 409
    assert "the lock did not prevent a clash" in caplog.text


def test_booking_a_slot_that_does_not_exist_gives_404(auth_client):
    response = auth_client.post("/api/bookings/", {"slot_id": 999999}, format="json")
    assert response.status_code == 404


@pytest.mark.django_db
def test_the_naive_booking_function_rejects_missing_and_taken_slots(user):
    """create_booking_naive() is kept as the 'before' picture of the race condition."""
    with pytest.raises(services.SlotNotFound):
        services.create_booking_naive(user, 999999)
    slot = SlotFactory()
    BookingFactory(slot=slot)  # somebody holds it
    with pytest.raises(services.SlotUnavailable):
        services.create_booking_naive(user, slot.id)


@pytest.mark.django_db
def test_the_naive_booking_function_can_pause_between_check_and_create(user, monkeypatch):
    slept = []
    monkeypatch.setattr(services.time, "sleep", slept.append)
    booking = services.create_booking_naive(user, SlotFactory().id, _pause=0.05)
    assert slept == [0.05] and booking.pk


@pytest.mark.django_db
def test_verify_endpoint_says_503_when_razorpay_keys_are_missing(auth_client, settings):
    settings.RAZORPAY_KEY_ID = ""
    settings.RAZORPAY_KEY_SECRET = ""
    response = auth_client.post(
        "/api/payments/verify/",
        {
            "razorpay_order_id": "order_1",
            "razorpay_payment_id": "pay_1",
            "razorpay_signature": "a" * 64,
        },
        format="json",
    )
    assert response.status_code == 503


@pytest.mark.django_db
def test_a_failed_payment_event_without_an_order_id_is_ignored(api_client, razorpay):
    body = b'{"event":"payment.failed","payload":{"payment":{"entity":{"id":"pay_1"}}}}'
    response = post_webhook(api_client, body, signature=webhook_signature(body))
    assert response.json() == {"status": "ignored_malformed"}


@pytest.mark.django_db
def test_verify_works_for_a_payment_created_through_the_factory(auth_client, user, razorpay):
    """Smoke test that the factories build objects the payment code accepts."""
    booking = BookingFactory(user=user, expires_at=timezone.now() + timedelta(minutes=5))
    payment = PaymentFactory(booking=booking)
    response = auth_client.post(
        "/api/payments/verify/",
        {
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": "pay_FACTORY",
            "razorpay_signature": checkout_signature(payment.razorpay_order_id, "pay_FACTORY"),
        },
        format="json",
    )
    assert response.status_code == 200 and response.json()["outcome"] == "confirmed"
