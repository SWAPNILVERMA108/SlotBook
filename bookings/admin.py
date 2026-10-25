from django.contrib import admin

from .models import Booking


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "slot", "status", "expires_at", "created_at")
    list_filter = ("status",)
    search_fields = ("user__username",)
    list_select_related = ("user", "slot__venue")  # avoids N+1 queries in the list page
    date_hierarchy = "created_at"
    raw_id_fields = ("user", "slot")  # avoids loading a huge dropdown
    readonly_fields = ("created_at", "updated_at")
    list_per_page = 50
