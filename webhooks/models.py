from django.db import models
from django.utils import timezone

from core.models import Merchant
from core.utils import new_id


def evt_id():
    return new_id("evt")


class WebhookEvent(models.Model):
    public_id = models.CharField(max_length=32, unique=True, default=evt_id, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="webhook_events")
    event_type = models.CharField(max_length=40, db_index=True)
    payload = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class WebhookDelivery(models.Model):
    """One row per event+endpoint; `attempts` counts tries. Worker picks rows whose next_attempt_at has passed."""

    STATUS = [("pending", "Pending"), ("delivered", "Delivered"), ("retrying", "Retrying"), ("failed", "Failed")]
    event = models.ForeignKey(WebhookEvent, on_delete=models.CASCADE, related_name="deliveries")
    url = models.URLField()
    status = models.CharField(max_length=10, choices=STATUS, default="pending", db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_response_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]


class DeliveryAttempt(models.Model):
    delivery = models.ForeignKey(WebhookDelivery, on_delete=models.CASCADE, related_name="attempt_log")
    attempt_no = models.PositiveSmallIntegerField()
    response_code = models.PositiveSmallIntegerField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["attempt_no"]
