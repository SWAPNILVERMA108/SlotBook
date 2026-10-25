import hashlib
import json
import logging

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services, webhooks
from .models import Payment
from .serializers import CreateOrderSerializer, VerifyPaymentSerializer

logger = logging.getLogger(__name__)


class CreateOrderView(APIView):
    """POST /api/payments/create-order/   body: {"booking_id": 1}

    Gives the browser everything Razorpay Checkout needs (order id, amount in paise, key id).
    """

    @extend_schema(request=CreateOrderSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request):
        serializer = CreateOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            data = services.create_order_for_booking(
                request.user, serializer.validated_data["booking_id"]
            )
        except services.BookingNotFound as error:
            return Response({"detail": str(error)}, status=status.HTTP_404_NOT_FOUND)
        except services.BookingNotPayable as error:
            return Response({"detail": str(error)}, status=status.HTTP_409_CONFLICT)
        except services.PaymentConfigError:
            return Response(
                {"detail": "Payments are not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except services.RazorpayUnavailable as error:
            return Response({"detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(data)


class VerifyPaymentView(APIView):
    """POST /api/payments/verify/   body: the 3 values Razorpay Checkout returned.

    1. check the signature (is this message really from Razorpay?)
    2. settle the payment (confirm the booking, or start a refund)
    """

    @extend_schema(request=VerifyPaymentSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request):
        serializer = VerifyPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            services.verify_checkout_signature(
                data["razorpay_order_id"], data["razorpay_payment_id"], data["razorpay_signature"]
            )
        except services.InvalidSignature as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except services.PaymentConfigError:
            return Response(
                {"detail": "Payments are not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # The order must belong to THIS user's booking (404 hides other people's orders).
        owned = Payment.objects.filter(
            razorpay_order_id=data["razorpay_order_id"], booking__user=request.user
        ).exists()
        if not owned:
            return Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)

        settlement = services.settle_successful_payment(
            data["razorpay_order_id"], data["razorpay_payment_id"]
        )
        return Response(
            {
                "outcome": settlement.outcome,
                "booking_id": settlement.booking.id,
                "booking_status": settlement.booking.status,
                "payment_status": settlement.payment.status,
            }
        )


# Razorpay's server is not a browser, so there is no CSRF cookie.
# The SIGNATURE is what protects this URL.
@csrf_exempt
@require_POST
def razorpay_webhook(request):
    """POST /api/payments/webhook/   (called by Razorpay, not by users)

    Answers:  200 = handled (or safely ignored, Razorpay stops retrying)
              400 = bad signature or unreadable body
              503 = we are not configured
              500 = our own error. Razorpay will retry later.
    """
    raw_body = request.body  # the exact bytes. Must be read BEFORE any JSON parsing.
    signature = request.headers.get("X-Razorpay-Signature", "")

    try:
        services.verify_webhook_signature(raw_body, signature)
    except services.PaymentConfigError:
        logger.error("Webhook received but RAZORPAY_WEBHOOK_SECRET is not set")
        return JsonResponse({"detail": "Webhook is not configured."}, status=503)
    except services.InvalidSignature:
        logger.warning("Webhook with an invalid signature rejected")
        return JsonResponse({"detail": "Invalid signature."}, status=400)

    try:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
    except ValueError:
        return JsonResponse({"detail": "Body is not valid JSON."}, status=400)

    # Razorpay sends a unique id for every event. If a client or proxy ever drops that header,
    # a hash of the body still recognises an identical re-delivery.
    event_id = (
        request.headers.get("X-Razorpay-Event-Id")
        or "sha256:" + hashlib.sha256(raw_body).hexdigest()
    )
    event_type = str(payload.get("event", ""))

    result = webhooks.handle_event(event_id, event_type, payload)
    return JsonResponse({"status": result})


def demo_checkout(request):
    """A tiny test page (DEBUG only) to try the whole payment flow with Razorpay TEST mode."""
    if not settings.DEBUG:
        raise Http404()
    return render(request, "payments/demo_checkout.html")
