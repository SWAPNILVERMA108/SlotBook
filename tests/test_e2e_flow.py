"""One test that walks through the WHOLE customer journey over the real HTTP API:
register -> login -> find a slot -> book -> pay -> confirmed -> cancel -> refund -> slot is free again.
If this test passes, all the main pieces work together.
"""

import pytest

from bookings.models import BookingStatus
from payments import services
from payments.models import Payment, PaymentStatus
from payments.tests.helpers import checkout_signature
from tests.factories import PASSWORD, SlotFactory, VenueFactory


@pytest.mark.django_db
def test_full_customer_journey(
    api_client, razorpay, refund_queue, django_capture_on_commit_callbacks
):
    slot = SlotFactory(venue=VenueFactory(city="Noida"), price="500.00")

    # 1. register and log in
    response = api_client.post(
        "/api/auth/register/",
        {"username": "swapnil", "email": "swapnil@example.com", "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 201
    response = api_client.post(
        "/api/auth/login/", {"username": "swapnil", "password": PASSWORD}, format="json"
    )
    assert response.status_code == 200
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.json()['access']}")

    # 2. the slot is listed
    response = api_client.get("/api/slots/", {"city": "noida"})
    assert [row["id"] for row in response.json()["results"]] == [slot.id]

    # 3. book it: PENDING with a hold, and it disappears from the list
    response = api_client.post("/api/bookings/", {"slot_id": slot.id}, format="json")
    assert response.status_code == 201
    booking_id = response.json()["id"]
    assert response.json()["status"] == "PENDING"
    assert api_client.get("/api/slots/").json()["count"] == 0

    # 4. create the Razorpay order (amount in paise)
    response = api_client.post(
        "/api/payments/create-order/", {"booking_id": booking_id}, format="json"
    )
    assert response.status_code == 200
    order = response.json()
    assert order["amount"] == 50000

    # 5. "pay", then verify the signature Razorpay would give the browser
    payment_id = "pay_JOURNEY1"
    response = api_client.post(
        "/api/payments/verify/",
        {
            "razorpay_order_id": order["order_id"],
            "razorpay_payment_id": payment_id,
            "razorpay_signature": checkout_signature(order["order_id"], payment_id),
        },
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "confirmed"

    # 6. my bookings shows it as CONFIRMED
    mine = api_client.get("/api/bookings/me/").json()["results"]
    assert [(b["id"], b["status"]) for b in mine] == [(booking_id, "CONFIRMED")]

    # 7. cancel: the booking is cancelled and the refund is queued in the same moment
    with django_capture_on_commit_callbacks(execute=True):
        response = api_client.post(f"/api/bookings/{booking_id}/cancel/")
    assert response.status_code == 200
    assert response.json()["status"] == BookingStatus.CANCELLED
    payment = Payment.objects.get()
    assert payment.status == PaymentStatus.REFUND_PENDING
    refund_queue.assert_called_once_with(payment.pk)

    # 8. the refund task sends the money back to Razorpay
    assert services.refund_payment(payment.pk) == "refunded"
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.REFUNDED
    assert razorpay.calls_to("/v1/payments/pay_JOURNEY1/refund")

    # 9. the slot can be booked by somebody else again
    assert api_client.get("/api/slots/").json()["count"] == 1
