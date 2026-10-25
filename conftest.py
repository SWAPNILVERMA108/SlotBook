"""Shared pytest fixtures. Every test file in this project can use them by name."""

from unittest import mock

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from payments import services as payment_services
from payments import tasks as payment_tasks
from payments.tests.helpers import KEY_ID, KEY_SECRET, WEBHOOK_SECRET, FakeRazorpaySession
from tests.factories import SlotFactory, UserFactory, VenueFactory


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Rate-limit counters live in the cache. Start every test with an empty one."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.fixture
def auth_client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def venue(db):
    return VenueFactory()


@pytest.fixture
def slot(venue):
    return SlotFactory(venue=venue)


@pytest.fixture
def razorpay(settings, monkeypatch):
    """Razorpay test keys plus a fake Razorpay SERVER. The real Razorpay library still runs.

    Returns the fake, so a test can look at every call that was made (fake.calls) or make
    Razorpay fail (fake.error_status = 400).
    """
    settings.RAZORPAY_KEY_ID = KEY_ID
    settings.RAZORPAY_KEY_SECRET = KEY_SECRET
    settings.RAZORPAY_WEBHOOK_SECRET = WEBHOOK_SECRET
    fake = FakeRazorpaySession()
    monkeypatch.setattr(payment_services, "make_session", lambda: fake)
    return fake


@pytest.fixture
def refund_queue(monkeypatch):
    """Replaces the Celery call that queues a refund, so no Redis is needed. Returns the mock."""
    queued = mock.Mock()
    monkeypatch.setattr(payment_tasks.process_refund, "delay", queued)
    return queued
