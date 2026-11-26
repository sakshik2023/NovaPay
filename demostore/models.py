from django.db import models
from django.utils import timezone

from core.utils import new_id


def store_order_id():
    return new_id("so")


class StoreOrder(models.Model):
    """An order inside the demo 'merchant' website (the merchant's own database)."""

    STATUS = [("awaiting_payment", "Awaiting payment"), ("paid", "Paid"), ("payment_failed", "Payment failed")]
    public_id = models.CharField(max_length=32, unique=True, default=store_order_id, editable=False)
    items = models.JSONField(default=list)
    total_paise = models.BigIntegerField()
    gateway_order_id = models.CharField(max_length=40, blank=True, db_index=True)
    gateway_payment_id = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="awaiting_payment")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]


class ReceivedWebhook(models.Model):
    """Log of webhooks the demo store has received (shows signature verification result)."""

    event_id = models.CharField(max_length=40, blank=True)
    event_type = models.CharField(max_length=40, blank=True)
    signature_valid = models.BooleanField(default=False)
    body = models.TextField()
    received_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-received_at", "-id"]
