from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from .utils import new_id, new_secret, sha256


def default_mid():
    return new_id("mid")


def default_whsec():
    return "whsec_" + new_secret(18)


class Merchant(models.Model):
    """A business that accepts payments through NovaPay."""

    KYC_CHOICES = [("pending", "Pending"), ("verified", "Verified"), ("rejected", "Rejected")]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="merchant")
    public_id = models.CharField(max_length=32, unique=True, default=default_mid, editable=False)
    business_name = models.CharField(max_length=120)
    business_type = models.CharField(max_length=60, default="Retail")
    website = models.URLField(blank=True)
    kyc_status = models.CharField(max_length=10, choices=KYC_CHOICES, default="verified")
    webhook_url = models.URLField(blank=True)
    webhook_secret = models.CharField(max_length=64, default=default_whsec)
    max_payment_paise = models.BigIntegerField(default=10_000_000, help_text="Fraud rule: single payment limit")
    brand_color = models.CharField(max_length=7, default="#6C5CE7")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.business_name

    def rotate_webhook_secret(self):
        self.webhook_secret = "whsec_" + new_secret(18)
        self.save(update_fields=["webhook_secret"])


class APIKey(models.Model):
    """Public key_id + hashed secret. The secret is shown exactly once at creation."""

    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, related_name="api_keys")
    key_id = models.CharField(max_length=40, unique=True)
    secret_hash = models.CharField(max_length=64)
    label = models.CharField(max_length=60, default="Default key")
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_active(self):
        return self.revoked_at is None

    @classmethod
    def generate(cls, merchant, label="Default key"):
        key_id = "np_test_" + new_secret(9)
        secret = "sk_" + new_secret(24)
        obj = cls.objects.create(merchant=merchant, key_id=key_id, secret_hash=sha256(secret), label=label)
        return obj, secret  # plaintext secret returned once, never stored

    def revoke(self):
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at"])


class IdempotencyRecord(models.Model):
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE)
    key = models.CharField(max_length=120)
    endpoint = models.CharField(max_length=120)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveIntegerField(default=0)
    response_body = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["merchant", "key"], name="uniq_idem_per_merchant")]


class AuditLog(models.Model):
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE, null=True, blank=True, related_name="audit_logs")
    actor = models.CharField(max_length=80)
    action = models.CharField(max_length=80)
    entity_type = models.CharField(max_length=40, blank=True)
    entity_id = models.CharField(max_length=60, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]


def audit(merchant, actor, action, entity=None, ip=None, **detail):
    return AuditLog.objects.create(
        merchant=merchant,
        actor=actor,
        action=action,
        entity_type=entity.__class__.__name__ if entity is not None else "",
        entity_id=str(getattr(entity, "public_id", getattr(entity, "pk", ""))) if entity is not None else "",
        detail=detail,
        ip=ip,
    )
