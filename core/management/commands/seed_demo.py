"""Create a demo merchant with ~2 weeks of realistic history so every dashboard page has data."""
import json
import random
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.exceptions import GatewayError
from core.models import APIKey, Merchant
from demostore.gateway_client import CRED_FILE
from ledger import services as ledger
from ledger.models import LedgerEntry, LedgerTransaction, Settlement
from payments import services
from payments.models import Order, Payment, PaymentEvent, Refund
from webhooks import services as hooks

OK_CARDS = ["4111111111111111", "5555555555554444", "4111111111111111"]
FAIL_CARDS = ["4000000000009995", "4000000000000069", "4000000000000002"]
AMOUNTS = [49900, 79900, 99900, 149900, 249900, 499900, 129900, 59900]
PRODUCTS = ["Python Mastery Course", "Django Weekend Workshop", "Data Science Bundle", "Resume Review",
            "1:1 Mentorship", "Certification Exam"]
EMAIL = "demo@novapay.test"


def backdate(payment, when):
    Order.objects.filter(pk=payment.order_id).update(created_at=when)
    Payment.objects.filter(pk=payment.pk).update(
        created_at=when, captured_at=when + timedelta(seconds=3) if payment.captured_at else None)
    PaymentEvent.objects.filter(payment=payment).update(created_at=when + timedelta(seconds=2))
    LedgerTransaction.objects.filter(payment=payment, kind="capture").update(created_at=when + timedelta(seconds=3))


def wipe(merchant):
    LedgerEntry.objects.filter(transaction__merchant=merchant).delete()
    LedgerTransaction.objects.filter(merchant=merchant).delete()
    Refund.objects.filter(merchant=merchant).delete()
    Payment.objects.filter(merchant=merchant).update(settlement=None)
    Settlement.objects.filter(merchant=merchant).delete()
    Payment.objects.filter(merchant=merchant).delete()
    Order.objects.filter(merchant=merchant).delete()


class Command(BaseCommand):
    help = f"Seed demo merchant ({EMAIL} / demo1234) with sample data"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing demo history and rebuild")

    def handle(self, *args, **opts):
        rnd = random.Random(42)
        user, _ = User.objects.get_or_create(username=EMAIL, defaults={"email": EMAIL})
        user.set_password("demo1234")
        user.save()
        m, _ = Merchant.objects.get_or_create(user=user, defaults={
            "business_name": "LearnHub Academy", "business_type": "Education",
            "website": "https://learnhub.example", "brand_color": "#e8590c"})
        if opts["reset"]:
            wipe(m)
        m.webhook_url = settings.GATEWAY_BASE_URL + "/demo-store/webhook/"
        m.save()
        m.api_keys.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
        key, secret = APIKey.generate(m, "Demo store key")
        CRED_FILE.write_text(json.dumps({"key_id": key.key_id, "secret": secret, "webhook_secret": m.webhook_secret}))

        if Payment.objects.filter(merchant=m).exists():
            self.stdout.write(self.style.WARNING("Demo history already present (use --reset to rebuild). Credentials refreshed."))
            return

        now = timezone.now()
        n = 0
        with hooks.suppressed():
            for day in range(14, -1, -1):
                for _ in range(rnd.randint(3, 7)):
                    when = now - timedelta(days=day, hours=rnd.randint(0, 20), minutes=rnd.randint(0, 59))
                    amount = rnd.choice(AMOUNTS)
                    order = services.create_order(
                        m, amount, receipt=f"INV-{1000 + n}", description=rnd.choice(PRODUCTS),
                        customer_email=f"student{n}@example.com", source=rnd.choice(["api", "api", "link"]))
                    roll = rnd.random()
                    ip = f"10.{rnd.randint(0, 250)}.{rnd.randint(0, 250)}.{rnd.randint(1, 250)}"
                    try:
                        if roll < 0.38:
                            p = services.start_payment(order, "upi", {"vpa": rnd.choice(["asha@okaxis", "rahul@ybl", "meera@paytm", "dev@oksbi"])}, ip=ip)
                        elif roll < 0.88:
                            p = services.start_payment(order, "card", {"card_number": rnd.choice(OK_CARDS), "expiry": "12/30", "cvv": "123"}, ip=ip)
                        elif roll < 0.97:
                            p = services.start_payment(order, "card", {"card_number": rnd.choice(FAIL_CARDS), "expiry": "12/30", "cvv": "123"}, ip=ip)
                        else:
                            p = services.start_payment(order, "upi", {"vpa": "failure@upi"}, ip=ip)
                    except GatewayError:
                        continue
                    backdate(p, when)
                    if p.status == "captured" and rnd.random() < 0.12:
                        r = services.refund_payment(p, amount // 2 if rnd.random() < .5 else None, "Customer request", actor="seed")
                        Refund.objects.filter(pk=r.pk).update(created_at=when + timedelta(hours=5))
                        LedgerTransaction.objects.filter(refund=r).update(created_at=when + timedelta(hours=5))
                    n += 1
                if day == 7:  # one settlement run, a week ago
                    stl = ledger.run_settlement(m)
                    if stl:
                        when = now - timedelta(days=7)
                        Settlement.objects.filter(pk=stl.pk).update(created_at=when)
                        LedgerTransaction.objects.filter(settlement=stl).update(created_at=when)
            # Risk demo: blocklisted card, and repeated failures on one card (-> review flag)
            for i in range(2):
                o = services.create_order(m, 199900, receipt=f"INV-RISK{i}", description="Data Science Bundle")
                services.start_payment(o, "card", {"card_number": "4000000000000119", "expiry": "12/30", "cvv": "123"}, ip="203.0.113.7")
            for i in range(4):
                o = services.create_order(m, 59900, receipt=f"INV-RETRY{i}", description="Resume Review")
                services.start_payment(o, "card", {"card_number": "4000000000009995", "expiry": "12/30", "cvv": "123"}, ip=f"198.51.100.{i + 1}")

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {Payment.objects.filter(merchant=m).count()} payments. Login: {EMAIL} / demo1234"))
