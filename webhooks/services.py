"""Webhook engine: signed events, persistent delivery queue, exponential-backoff retries.

Signature header:  X-NovaPay-Signature: t=<unix>,v1=<hex>
  v1 = HMAC_SHA256(webhook_secret, f"{t}.{raw_body}")      (timestamp prevents replay)
"""
import json
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.utils import hmac_sha256, safe_equal
from .models import DeliveryAttempt, WebhookDelivery, WebhookEvent

_suppress = {"on": False}


@contextmanager
def suppressed():
    """Used by the seeder so historic demo data does not spam the queue."""
    _suppress["on"] = True
    try:
        yield
    finally:
        _suppress["on"] = False


def sign(secret: str, body: str, ts: int = None) -> str:
    ts = ts or int(time.time())
    return f"t={ts},v1={hmac_sha256(secret, f'{ts}.{body}')}"


def verify_signature(secret: str, body: str, header: str, tolerance: int = None) -> bool:
    tolerance = settings.WEBHOOK_SIGNATURE_TOLERANCE if tolerance is None else tolerance
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, sig = int(parts["t"]), parts["v1"]
    except Exception:
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    return safe_equal(sig, hmac_sha256(secret, f"{ts}.{body}"))


def emit(merchant, event_type: str, payload: dict):
    """Record an event and queue delivery. Safe to call inside a transaction (queued row commits with it)."""
    if _suppress["on"]:
        return None
    event = WebhookEvent.objects.create(merchant=merchant, event_type=event_type, payload=payload)
    if merchant.webhook_url:
        WebhookDelivery.objects.create(event=event, url=merchant.webhook_url)
    return event


def _body(event) -> str:
    return json.dumps({"id": event.public_id, "type": event.event_type,
                       "created_at": int(event.created_at.timestamp()), "data": event.payload},
                      separators=(",", ":"), sort_keys=True)


def attempt_delivery(delivery: WebhookDelivery) -> WebhookDelivery:
    event = delivery.event
    body = _body(event)
    sig = sign(event.merchant.webhook_secret, body)
    req = urllib.request.Request(delivery.url, data=body.encode(), method="POST", headers={
        "Content-Type": "application/json", "X-NovaPay-Signature": sig,
        "X-NovaPay-Event": event.event_type, "User-Agent": "NovaPay-Webhook/1.0"})
    started = time.time()
    code, error = None, ""
    try:
        with urllib.request.urlopen(req, timeout=settings.WEBHOOK_TIMEOUT_SECONDS) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code, error = e.code, f"HTTP {e.code}"
    except Exception as e:  # connection refused, timeout, DNS ...
        error = f"{type(e).__name__}: {e}"[:290]
    duration = int((time.time() - started) * 1000)

    delivery.attempts += 1
    delivery.last_response_code = code
    delivery.last_error = error
    DeliveryAttempt.objects.create(delivery=delivery, attempt_no=delivery.attempts, response_code=code,
                                   error=error, duration_ms=duration)
    if code is not None and 200 <= code < 300:
        delivery.status = "delivered"
        delivery.delivered_at = timezone.now()
        delivery.last_error = ""
    else:
        backoff = settings.WEBHOOK_BACKOFF_SECONDS
        if delivery.attempts > len(backoff):
            delivery.status = "failed"
        else:
            delivery.status = "retrying"
            delivery.next_attempt_at = timezone.now() + timedelta(seconds=backoff[delivery.attempts - 1])
    delivery.save()
    return delivery


def process_due(limit: int = 50) -> int:
    """Deliver everything that is due. Returns number of attempts made."""
    due = WebhookDelivery.objects.filter(status__in=["pending", "retrying"],
                                         next_attempt_at__lte=timezone.now()).select_related("event__merchant")[:limit]
    n = 0
    for d in list(due):
        # claim the row so two workers don't send the same delivery
        claimed = WebhookDelivery.objects.filter(pk=d.pk, attempts=d.attempts, status=d.status).update(
            next_attempt_at=timezone.now() + timedelta(seconds=30))
        if not claimed:
            continue
        d.refresh_from_db()
        attempt_delivery(d)
        n += 1
    return n


def retry_now(delivery: WebhookDelivery):
    delivery.status = "pending"
    delivery.next_attempt_at = timezone.now()
    delivery.save(update_fields=["status", "next_attempt_at"])
    return attempt_delivery(delivery)
