from django.core.management.base import BaseCommand

from payments.services import expire_stale_orders


class Command(BaseCommand):
    help = "Mark unpaid orders past their expiry as expired"

    def handle(self, *a, **o):
        self.stdout.write(f"Expired {expire_stale_orders()} order(s)")
