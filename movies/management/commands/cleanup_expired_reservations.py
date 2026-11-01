from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from movies.models import SeatReservation


class Command(BaseCommand):
    help = "Delete expired reservations for seats that have not been booked."

    def handle(self, *args, **options):
        now = timezone.now()

        with transaction.atomic():
            expired = SeatReservation.objects.filter(
                expires_at__lte=now,
                seat__is_booked=False,
            )
            count = expired.count()
            expired.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Expired unbooked reservations removed: {count}"
            )
        )
