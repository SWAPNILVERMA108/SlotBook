from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    # Show the phone field in the admin edit page
    fieldsets = UserAdmin.fieldsets + (("Extra info", {"fields": ("phone",)}),)
    list_display = ("username", "email", "phone", "is_staff")
