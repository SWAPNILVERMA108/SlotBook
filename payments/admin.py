from django.contrib import admin

from .models import Payment, WebhookEvent


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("razorpay_order_id", "booking", "amount", "status", "status_note", "created_at")
    list_filter = ("status",)
    list_select_related = ("booking__user", "booking__slot__venue")
    search_fields = ("razorpay_order_id", "razorpay_payment_id", "razorpay_refund_id")
    raw_id_fields = ("booking",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    """A read-only log. Nobody should edit or add these by hand."""

    list_display = ("event_id", "event_type", "result", "created_at")
    list_filter = ("event_type", "result")
    search_fields = ("event_id",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
