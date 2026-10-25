from django.urls import path

from .views import CreateOrderView, VerifyPaymentView, demo_checkout, razorpay_webhook

urlpatterns = [
    path("create-order/", CreateOrderView.as_view(), name="payment-create-order"),
    path("verify/", VerifyPaymentView.as_view(), name="payment-verify"),
    path("webhook/", razorpay_webhook, name="payment-webhook"),
    path("demo/", demo_checkout, name="payment-demo"),
]
