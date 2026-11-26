import hashlib
import hmac
import secrets
import uuid
from decimal import Decimal, ROUND_HALF_UP


def new_id(prefix: str) -> str:
    """Stripe-like prefixed public id, e.g. pay_3f9a1c0b7d2e4a."""
    return f"{prefix}_{uuid.uuid4().hex[:14]}"


def new_secret(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hmac_sha256(key: str, message: str) -> str:
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def pct(amount_paise: int, percent) -> int:
    """percent of an integer paise amount, rounded half-up, returned as int paise."""
    return int((Decimal(amount_paise) * Decimal(percent) / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_inr(paise: int) -> str:
    """Indian digit grouping: 1234567 paise -> 12,345.67"""
    paise = int(paise or 0)
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    rupees, p = divmod(paise, 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"{sign}{s}.{p:02d}"


def client_ip(request) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")
