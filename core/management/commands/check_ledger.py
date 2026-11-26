from django.core.management.base import BaseCommand, CommandError

from ledger.services import trial_balance, verify_ledger


class Command(BaseCommand):
    help = "Verify the double-entry invariants (every transaction balances; total debits == total credits)"

    def handle(self, *a, **o):
        bad = verify_ledger()
        _, d, c = trial_balance()
        if bad or d != c:
            raise CommandError(f"LEDGER OUT OF BALANCE: unbalanced txns={bad} debits={d} credits={c}")
        self.stdout.write(self.style.SUCCESS(f"Ledger OK — debits {d} == credits {c} paise"))
