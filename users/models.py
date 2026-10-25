from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Our own User model. It has everything Django's default user has,
    plus a phone number. We can add more fields later."""

    phone = models.CharField(max_length=15, blank=True)

    def __str__(self):
        return self.username
