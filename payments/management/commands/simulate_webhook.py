"""Send a correctly SIGNED fake Razorpay webhook to your own running server.

Why? Real webhooks need a public URL (ngrok) and a Razorpay dashboard setup. This command lets
you test the whole Day 11 logic on your laptop in seconds, and test duplicates too.

Examples (server must be running: python manage.py runserver):

  # 1. Book a slot in the API/admin, then make a FAKE payment row for that booking and "pay" it:
  python manage.py simulate_webhook --booking 5 --fake

  # 2. Same event delivered 3 times (the 2nd and 3rd must be answered "duplicate_ignored"):
  python manage.py simulate_webhook --booking 5 --fake --repeat 3

  # 3. A real order you created through /api/payments/create-order/:
  python manage.py simulate_webhook --order order_Abc123 --payment pay_Xyz789

  # 4. A failed payment attempt:
  python manage.py simulate_webhook --order order_Abc123 --event payment.failed

It uses RAZORPAY_WEBHOOK_SECRET from your settings, so the secret in .env must be set.
"""

import hashlib
import hmac
import json
import time
import uuid

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from bookings.models import Booking
from payments.models import Payment
from payments.services import rupees_to_paise


class Command(BaseCommand):
    help = "Sends a signed fake Razorpay webhook to your local server (for testing)."

    def add_arguments(self, parser):
        parser.add_argument("--order", help="razorpay order id of an existing Payment row")
        parser.add_argument(
            "--booking", type=int, help="with --fake: booking id to create a fake Payment for"
        )
        parser.add_argument(
            "--fake", action="store_true", help="create a fake Payment row (no Razorpay call)"
        )
        parser.add_argument("--payment", help="razorpay payment id (default: generated)")
        parser.add_argument(
            "--event",
            default="payment.captured",
            help="payment.captured, order.paid, payment.failed",
        )
        parser.add_argument(
            "--amount", type=int, help="amount in paise (default: the payment's amount)"
        )
        parser.add_argument(
            "--repeat", type=int, default=1, help="send the SAME event this many times"
        )
        parser.add_argument("--url", default="http://127.0.0.1:8000/api/payments/webhook/")
        parser.add_argument(
            "--force", action="store_true", help="allow this when DEBUG=False (not for production)"
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "Refusing to run: DEBUG is False. This is a testing tool (it can create fake "
                "payments). Use --force only on a test server."
            )
        secret = getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
        if not secret:
            raise CommandError("Set RAZORPAY_WEBHOOK_SECRET in your .env first.")

        payment = self.find_or_create_payment(options)
        payment_id = options["payment"] or f"pay_SIM{uuid.uuid4().hex[:12]}"
        amount = (
            options["amount"] if options["amount"] is not None else rupees_to_paise(payment.amount)
        )

        payload = {
            "entity": "event",
            "account_id": "acc_simulated",
            "event": options["event"],
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "entity": "payment",
                        "amount": amount,
                        "currency": payment.currency,
                        "status": "failed" if options["event"] == "payment.failed" else "captured",
                        "order_id": payment.razorpay_order_id,
                        "error_description": "Simulated failure",
                    }
                }
            },
            "created_at": int(time.time()),
        }
        # The signature is calculated over the exact bytes we send, so build them ONCE.
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        event_id = f"evt_SIM{uuid.uuid4().hex[:12]}"  # one id for all repeats = a true duplicate

        self.stdout.write(
            f"order={payment.razorpay_order_id} payment={payment_id} "
            f"event={options['event']} event_id={event_id}"
        )
        for number in range(1, options["repeat"] + 1):
            try:
                response = requests.post(
                    options["url"],
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Razorpay-Signature": signature,
                        "X-Razorpay-Event-Id": event_id,
                    },
                    timeout=10,
                )
            except requests.RequestException as error:
                raise CommandError(f"Could not reach {options['url']}: {error}") from error
            self.stdout.write(
                f"  delivery {number}: HTTP {response.status_code} {response.text.strip()}"
            )

    def find_or_create_payment(self, options):
        if options["order"]:
            try:
                return Payment.objects.get(razorpay_order_id=options["order"])
            except Payment.DoesNotExist:
                raise CommandError(f"No payment with order id {options['order']}") from None
        if options["fake"] and options["booking"]:
            try:
                booking = Booking.objects.select_related("slot").get(pk=options["booking"])
            except Booking.DoesNotExist:
                raise CommandError(f"No booking {options['booking']}") from None
            if booking.slot.price <= 0:
                raise CommandError("This slot is free, it needs no payment.")
            return Payment.objects.create(
                booking=booking,
                razorpay_order_id=f"order_SIM{uuid.uuid4().hex[:12]}",
                amount=booking.slot.price,
            )
        raise CommandError("Give --order ORDER_ID, or --booking BOOKING_ID --fake")
