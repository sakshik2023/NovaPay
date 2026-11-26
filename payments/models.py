from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.exceptions import InvalidTransition
from core.models import Merchant
from core.utils import new_id


def order_id():
    return new_id("order")


def payment_id():
    return new_id("pay")


def refund_id():
    return new_id("rfnd")


def default_expiry():
    return timezone.now() + timedelta(hours=settings.ORDER_EXPIRY_HOURS)


class Order(models.Model):
    STATUS = [("created", "Created"), ("attempted", "Attempted"), ("paid", "Paid"), ("expired", "Expired")]

    public_id = models.CharField(max_length=32, unique=True, default=order_id, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="orders")
    amount = models.BigIntegerField(help_text="Amount in paise")
    currency = models.CharField(max_length=3, default="INR")
    receipt = models.CharField(max_length=60, blank=True)
    description = models.CharField(max_length=200, blank=True)
    notes = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default="created", db_index=True)
    callback_url = models.URLField(blank=True)
    customer_name = models.CharField(max_length=80, blank=True)
    customer_email = models.EmailField(blank=True)
    source = models.CharField(max_length=10, default="api")  # api | link | store
    expires_at = models.DateTimeField(default=default_expiry)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="order_amount_positive")]
        indexes = [models.Index(fields=["merchant", "-created_at"])]

    def __str__(self):
        return self.public_id

    @property
    def is_expired(self):
        return self.status == "expired" or (self.status != "paid" and timezone.now() > self.expires_at)


class Payment(models.Model):
    STATUS = [
        ("pending", "Pending"),
        ("captured", "Captured"),
        ("failed", "Failed"),
        ("partially_refunded", "Partially refunded"),
        ("refunded", "Refunded"),
    ]
    METHODS = [("card", "Card"), ("upi", "UPI")]
    # State machine: the ONLY legal status changes in the system
    ALLOWED = {
        "pending": {"captured", "failed"},
        "captured": {"partially_refunded", "refunded"},
        "partially_refunded": {"partially_refunded", "refunded"},
    }

    public_id = models.CharField(max_length=32, unique=True, default=payment_id, editable=False)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="payments")
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="payments")
    amount = models.BigIntegerField()
    method = models.CharField(max_length=6, choices=METHODS)
    status = models.CharField(max_length=20, choices=STATUS, default="pending", db_index=True)

    # Never the PAN: only last4 + network + a salted fingerprint for velocity rules
    card_last4 = models.CharField(max_length=4, blank=True)
    card_network = models.CharField(max_length=12, blank=True)
    card_fingerprint = models.CharField(max_length=64, blank=True, db_index=True)
    upi_vpa = models.CharField(max_length=80, blank=True)
    customer_email = models.EmailField(blank=True)

    failure_code = models.CharField(max_length=40, blank=True)
    failure_reason = models.CharField(max_length=200, blank=True)
    bank_reference = models.CharField(max_length=40, blank=True)

    fee = models.BigIntegerField(default=0)
    gst = models.BigIntegerField(default=0)
    net_amount = models.BigIntegerField(default=0)
    refunded_amount = models.BigIntegerField(default=0)

    fraud_score = models.PositiveSmallIntegerField(default=0)
    fraud_decision = models.CharField(max_length=8, default="allow")
    fraud_reasons = models.JSONField(default=list, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    otp_attempts = models.PositiveSmallIntegerField(default=0)

    settlement = models.ForeignKey("ledger.Settlement", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="payments")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    captured_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_positive"),
            models.CheckConstraint(condition=Q(refunded_amount__lte=F("amount")), name="refund_not_above_amount"),
        ]
        indexes = [
            models.Index(fields=["merchant", "-created_at"]),
            models.Index(fields=["merchant", "status"]),
        ]

    def __str__(self):
        return self.public_id

    @property
    def refundable(self):
        return self.amount - self.refunded_amount if self.status in ("captured", "partially_refunded") else 0

    @property
    def instrument(self):
        if self.method == "card":
            return f"{self.card_network.title()} •••• {self.card_last4}"
        return self.upi_vpa

    def transition(self, new_status):
        """Single place that enforces the payment state machine."""
        if new_status not in self.ALLOWED.get(self.status, set()):
            raise InvalidTransition(self.status, new_status)
        self.status = new_status


class PaymentEvent(models.Model):
    """Timeline shown on the payment detail page."""

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=30)
    message = models.CharField(max_length=240)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]


class Refund(models.Model):
    public_id = models.CharField(max_length=32, unique=True, default=refund_id, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="refunds")
    amount = models.BigIntegerField()
    reason = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=12, default="processed")
    settlement = models.ForeignKey("ledger.Settlement", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="refunds")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="refund_amount_positive")]
