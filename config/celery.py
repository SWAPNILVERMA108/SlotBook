"""Celery setup: the "background worker" of our project.

Big picture:
    Django app --(puts a task message)--> Redis (the broker) --(worker picks it up)--> worker
The web request returns immediately, and the slow work happens in the background.
"""
import os

from celery import Celery

# Tell Celery which Django settings to use (same value manage.py uses).
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("slotbook")

# Read every setting that starts with CELERY_ from config/settings.py.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Look for a tasks.py file in every installed app (bookings/tasks.py, payments/tasks.py, ...).
app.autodiscover_tasks()
