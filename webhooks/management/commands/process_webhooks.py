import time

from django.core.management.base import BaseCommand

from webhooks.services import process_due


class Command(BaseCommand):
    help = "Deliver due webhooks. Use --loop to run as a worker process."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--interval", type=int, default=2)

    def handle(self, *a, **o):
        while True:
            n = process_due()
            if n:
                self.stdout.write(f"attempted {n} deliveries")
            if not o["loop"]:
                break
            time.sleep(o["interval"])
