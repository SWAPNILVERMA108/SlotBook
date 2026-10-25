from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.utils import timezone

from venues.models import Slot, Venue

User = get_user_model()

# A deliberately PUBLIC demo password (the command refuses to run on a real server).
DEMO_PASSWORD = "DemoPass#123"  # nosec B105


def check_database_is_ready():
    """Stops with a clear message if the tables do not exist yet.

    "relation venues_venue does not exist" always means: the migrations were not created
    (makemigrations) or not applied (migrate). Instead of a 60-line error we say exactly that.
    """
    existing_tables = set(connection.introspection.table_names())
    needed = {model._meta.db_table: model._meta.app_label for model in (User, Venue, Slot)}
    missing = sorted(table for table in needed if table not in existing_tables)
    if not missing:
        return

    # disk_migrations holds one entry per migration FILE found on disk, as (app_label, name).
    loader = MigrationLoader(connection, ignore_no_migrations=True)
    apps_with_files = {app_label for app_label, _name in loader.disk_migrations}
    apps_without_files = sorted({needed[t] for t in missing if needed[t] not in apps_with_files})
    if apps_without_files:
        hint = (
            f"There are no migration files yet for: {', '.join(apps_without_files)}.\n"
            "  1) python manage.py makemigrations\n"
            "  2) python manage.py migrate"
        )
    else:
        hint = "The migration files exist but were not applied.\n  python manage.py migrate"
    raise CommandError(
        f"The database is not ready. Missing tables: {', '.join(missing)}.\n{hint}\n"
        "Then run  python manage.py seed_demo  again."
    )


class Command(BaseCommand):
    help = "Creates demo users, one venue and hourly slots (17:00-21:00) for the next N days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=7, help="how many days of slots (default 7)"
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="allow this when DEBUG=False (it creates users with a PUBLIC password!)",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "Refusing to run: DEBUG is False, so this looks like a real server, and this "
                "command creates users with a publicly known password.\n"
                "For a demo deployment run it again with --force (delete those users afterwards)."
            )
        check_database_is_ready()

        # One transaction: either everything is created, or nothing (no half-filled demo data).
        with transaction.atomic():
            owner = self.get_user("demo_owner")
            self.get_user("alice")
            self.get_user("bob")

            venue, _ = Venue.objects.get_or_create(
                owner=owner,
                name="Demo Turf",
                defaults={"address": "Sector 62", "city": "Noida"},
            )

            today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
            created = 0
            for day in range(1, options["days"] + 1):
                for hour in (17, 18, 19, 20):
                    start = today + timedelta(days=day, hours=hour)
                    _, was_created = Slot.objects.get_or_create(
                        venue=venue,
                        start_time=start,
                        defaults={
                            "end_time": start + timedelta(hours=1),
                            "price": Decimal("600.00"),
                        },
                    )
                    created += was_created

        self.stdout.write(self.style.SUCCESS(f"Done. {created} new slots created."))
        self.stdout.write(f"Demo users: demo_owner, alice, bob   password: {DEMO_PASSWORD}")

    def get_user(self, username):
        user, created = User.objects.get_or_create(
            username=username, defaults={"email": f"{username}@example.com"}
        )
        if created:
            user.set_password(DEMO_PASSWORD)
            user.save()
        return user
