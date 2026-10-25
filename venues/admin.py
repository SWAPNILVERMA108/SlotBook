from django.contrib import admin

from .models import Slot, Venue


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "owner", "is_active", "created_at")
    list_filter = ("city", "is_active")
    search_fields = ("name", "address", "owner__username")
    list_select_related = ("owner",)  # one JOIN instead of one query per row


@admin.register(Slot)
class SlotAdmin(admin.ModelAdmin):
    list_display = ("venue", "start_time", "end_time", "price", "is_active")
    list_filter = ("is_active", "venue")
    list_select_related = ("venue",)  # Slot.__str__ reads venue.name, so avoid N+1 queries
    date_hierarchy = "start_time"
    list_per_page = 50
