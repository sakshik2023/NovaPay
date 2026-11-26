from django.core.management.base import BaseCommand

from core.models import Merchant
from ledger.services import run_settlement


class Command(BaseCommand):
    help = "Settle every merchant (schedule daily with cron / Task Scheduler)"

    def handle(self, *a, **o):
        for m in Merchant.objects.all():
            s = run_settlement(m)
            self.stdout.write(f"{m.business_name}: " + (f"settled {s.net} paise ({s.public_id})" if s else "nothing to settle"))
