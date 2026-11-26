"""Rules-based fraud engine.  Each rule returns (points, reason) or None.
Score >= 80 -> block, 40..79 -> review (allowed but flagged), else allow."""
from datetime import timedelta
from dataclasses import dataclass, field

from django.utils import timezone

from .models import Payment
from . import simulator

BLOCK_AT = 80
REVIEW_AT = 40


@dataclass
class FraudResult:
    score: int = 0
    reasons: list = field(default_factory=list)

    @property
    def decision(self):
        if self.score >= BLOCK_AT:
            return "block"
        if self.score >= REVIEW_AT:
            return "review"
        return "allow"


def evaluate(merchant, amount, ip, fingerprint="", raw_card="") -> FraudResult:
    now = timezone.now()
    res = FraudResult()

    def add(points, reason):
        res.score = min(100, res.score + points)
        res.reasons.append(reason)

    # 1. Blocklisted card
    if raw_card and raw_card in simulator.BLOCKED_CARDS:
        add(100, "Card is on the NovaPay blocklist")

    # 2. Merchant single-payment limit
    if amount > merchant.max_payment_paise:
        add(100, "Amount exceeds the merchant's per-payment limit")
    elif amount >= 5_000_000:  # Rs 50,000
        add(20, "High-value transaction (>= Rs 50,000)")

    # 3. IP velocity: >5 attempts in 10 minutes
    if ip:
        recent_ip = Payment.objects.filter(ip=ip, created_at__gte=now - timedelta(minutes=10)).count()
        if recent_ip >= 5:
            add(45, f"{recent_ip} attempts from this IP in 10 minutes")

    if fingerprint:
        hour = now - timedelta(hours=1)
        # 4. Repeated failures on same card
        fails = Payment.objects.filter(card_fingerprint=fingerprint, status="failed", created_at__gte=hour).count()
        if fails >= 3:
            add(50, f"Card failed {fails} times in the last hour")
        # 5. Same card across many merchants
        merchants = (Payment.objects.filter(card_fingerprint=fingerprint, created_at__gte=hour)
                     .exclude(merchant=merchant).values("merchant").distinct().count())
        if merchants >= 2:
            add(35, f"Card used at {merchants + 1} different merchants within an hour")

    return res
