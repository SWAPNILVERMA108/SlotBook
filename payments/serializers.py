from rest_framework import serializers


class CreateOrderSerializer(serializers.Serializer):
    booking_id = serializers.IntegerField(min_value=1)


class VerifyPaymentSerializer(serializers.Serializer):
    """Exactly the three values Razorpay Checkout gives the browser after a payment."""

    # max_length stops absurdly long input from reaching the signature code.
    razorpay_order_id = serializers.CharField(max_length=64)
    razorpay_payment_id = serializers.CharField(max_length=64)
    razorpay_signature = serializers.CharField(max_length=128)
