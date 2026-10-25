"""The two helper commands: seed_demo (demo data) and simulate_webhook (fake Razorpay)."""

import json
from decimal import Decimal
from unittest import mock

import pytest
import requests
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from payments.models import Payment
from payments.tests.helpers import WEBHOOK_SECRET, webhook_signature
from tests.factories import BookingFactory, SlotFactory
from venues.models import Slot, Venue

User = get_user_model()


# ---------------- seed_demo ----------------
@pytest.mark.django_db
def test_seed_demo_creates_users_venue_and_slots(settings):
    settings.DEBUG = True
    call_command("seed_demo", "--days", "2")
    assert Venue.objects.get().name == "Demo Turf"
    assert Slot.objects.count() == 8  # 2 days x 4 hours
    alice = User.objects.get(username="alice")
    assert alice.check_password("DemoPass#123")


@pytest.mark.django_db
def test_seed_demo_can_run_twice_without_duplicates(settings):
    settings.DEBUG = True
    call_command("seed_demo", "--days", "1")
    call_command("seed_demo", "--days", "1")
    assert Slot.objects.count() == 4
    assert User.objects.filter(username="alice").count() == 1


@pytest.mark.django_db
def test_seed_demo_refuses_on_a_real_server(settings):
    """DEBUG=False means a real server. This command makes users with a PUBLIC password."""
    settings.DEBUG = False
    with pytest.raises(CommandError, match="Refusing to run"):
        call_command("seed_demo")
    assert not User.objects.filter(username="alice").exists()


@pytest.mark.django_db
def test_seed_demo_force_flag_allows_a_demo_deployment(settings):
    settings.DEBUG = False
    call_command("seed_demo", "--force", "--days", "1")
    assert Slot.objects.count() == 4


@pytest.mark.django_db
def test_seed_demo_explains_what_to_do_when_tables_are_missing(settings):
    settings.DEBUG = True
    with mock.patch("django.db.connection.introspection.table_names", return_value=[]):
        with pytest.raises(CommandError) as error:
            call_command("seed_demo")
    message = str(error.value)
    assert "database is not ready" in message and "migrate" in message


@pytest.mark.django_db
def test_seed_demo_tells_you_to_run_makemigrations_when_there_are_no_migration_files(settings):
    settings.DEBUG = True
    loader = mock.Mock(disk_migrations={})  # "no migration files found for any app"
    with (
        mock.patch("django.db.connection.introspection.table_names", return_value=[]),
        mock.patch("bookings.management.commands.seed_demo.MigrationLoader", return_value=loader),
        pytest.raises(CommandError) as error,
    ):
        call_command("seed_demo")
    assert "makemigrations" in str(error.value)


# ---------------- simulate_webhook ----------------
class FakeServerAnswer:
    status_code = 200
    text = '{"status": "ok"}'


@pytest.fixture
def secret(settings):
    settings.DEBUG = True
    settings.RAZORPAY_WEBHOOK_SECRET = WEBHOOK_SECRET


@pytest.mark.django_db
def test_simulate_webhook_sends_a_correctly_signed_request(secret, capsys):
    booking = BookingFactory()
    with mock.patch("requests.post", return_value=FakeServerAnswer()) as post:
        call_command("simulate_webhook", "--booking", str(booking.id), "--fake")

    payment = Payment.objects.get()
    assert payment.booking == booking and payment.amount == Decimal("500.00")
    body = post.call_args.kwargs["data"]
    headers = post.call_args.kwargs["headers"]
    assert headers["X-Razorpay-Signature"] == webhook_signature(body)  # signed like Razorpay does
    entity = json.loads(body)["payload"]["payment"]["entity"]
    assert entity["order_id"] == payment.razorpay_order_id
    assert entity["amount"] == 50000
    assert "HTTP 200" in capsys.readouterr().out


@pytest.mark.django_db
def test_simulate_webhook_repeat_sends_the_same_event_id_every_time(secret):
    booking = BookingFactory()
    with mock.patch("requests.post", return_value=FakeServerAnswer()) as post:
        call_command("simulate_webhook", "--booking", str(booking.id), "--fake", "--repeat", "3")
    assert post.call_count == 3
    event_ids = {call.kwargs["headers"]["X-Razorpay-Event-Id"] for call in post.call_args_list}
    bodies = {call.kwargs["data"] for call in post.call_args_list}
    assert len(event_ids) == 1 and len(bodies) == 1  # a TRUE duplicate delivery


@pytest.mark.django_db
def test_simulate_webhook_can_send_a_failed_payment_for_an_existing_order(secret):
    payment = Payment.objects.create(
        booking=BookingFactory(), razorpay_order_id="order_X1", amount=Decimal("500")
    )
    with mock.patch("requests.post", return_value=FakeServerAnswer()) as post:
        call_command(
            "simulate_webhook",
            "--order",
            payment.razorpay_order_id,
            "--event",
            "payment.failed",
            "--amount",
            "123",
        )
    payload = json.loads(post.call_args.kwargs["data"])
    assert payload["event"] == "payment.failed"
    assert payload["payload"]["payment"]["entity"]["status"] == "failed"
    assert payload["payload"]["payment"]["entity"]["amount"] == 123


@pytest.mark.django_db
def test_simulate_webhook_refuses_on_a_real_server(settings):
    settings.DEBUG = False
    settings.RAZORPAY_WEBHOOK_SECRET = WEBHOOK_SECRET
    with pytest.raises(CommandError, match="Refusing to run"):
        call_command("simulate_webhook", "--order", "order_X")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "arguments, message",
    [
        (["--order", "order_DOES_NOT_EXIST"], "No payment with order id"),
        (["--booking", "999999", "--fake"], "No booking"),
        ([], "Give --order"),
    ],
)
def test_simulate_webhook_gives_clear_errors(secret, arguments, message):
    with pytest.raises(CommandError, match=message):
        call_command("simulate_webhook", *arguments)


@pytest.mark.django_db
def test_simulate_webhook_needs_the_webhook_secret(settings):
    settings.DEBUG = True
    settings.RAZORPAY_WEBHOOK_SECRET = ""
    with pytest.raises(CommandError, match="RAZORPAY_WEBHOOK_SECRET"):
        call_command("simulate_webhook", "--order", "order_X")


@pytest.mark.django_db
def test_simulate_webhook_refuses_free_slots(secret):
    booking = BookingFactory(slot=SlotFactory(price=Decimal("0.00")))
    with pytest.raises(CommandError, match="free"):
        call_command("simulate_webhook", "--booking", str(booking.id), "--fake")


@pytest.mark.django_db
def test_simulate_webhook_reports_an_unreachable_server_clearly(secret):
    booking = BookingFactory()
    with mock.patch("requests.post", side_effect=requests.ConnectionError("refused")):
        with pytest.raises(CommandError, match="Could not reach"):
            call_command("simulate_webhook", "--booking", str(booking.id), "--fake")
