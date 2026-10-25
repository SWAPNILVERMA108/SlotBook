"""The race_demo command, run against a REAL server (pytest's live_server) over real HTTP."""

from io import StringIO
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from bookings.models import Booking
from venues.models import Slot, Venue


@pytest.mark.django_db(transaction=True)
def test_race_demo_passes_against_a_live_server_and_cleans_up(live_server):
    out = StringIO()
    call_command("race_demo", "--users", "12", "--base-url", live_server.url, stdout=out)
    text = out.getvalue()
    assert "HTTP 201 (booked):         1" in text
    assert "HTTP 409 (already taken):  11" in text
    assert "PASS" in text
    # nothing is left behind
    assert not Booking.objects.exists() and not Slot.objects.exists() and not Venue.objects.exists()
    assert not get_user_model().objects.filter(username__startswith="race_").exists()


@pytest.mark.django_db(transaction=True)
def test_race_demo_can_keep_its_data(live_server):
    call_command(
        "race_demo", "--users", "4", "--base-url", live_server.url, "--keep", stdout=StringIO()
    )
    assert Booking.objects.count() == 1 and Slot.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_race_demo_reports_failure_when_the_server_misbehaves():
    """If two requests were ever both answered 201, the demo must say FAIL (and exit with an error)."""
    out = StringIO()
    with mock.patch("bookings.management.commands.race_demo.post_booking", return_value=201):
        with pytest.raises(CommandError, match="Race demo failed"):
            call_command("race_demo", "--users", "3", stdout=out)
    assert "FAIL" in out.getvalue()


@pytest.mark.django_db(transaction=True)
def test_race_demo_cleans_up_even_when_the_server_is_unreachable():
    out = StringIO()
    with pytest.raises(CommandError):
        call_command("race_demo", "--users", "3", "--base-url", "http://127.0.0.1:1", stdout=out)
    assert "error: ConnectionError" in out.getvalue()
    assert not Slot.objects.exists()


def test_race_demo_needs_at_least_two_users():
    with pytest.raises(CommandError, match="at least 2"):
        call_command("race_demo", "--users", "1")
