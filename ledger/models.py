from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import Merchant
from core.utils import new_id


def txn_id():
    return new_id("txn")


def stl_id():
    return new_id("setl")


class LedgerAccount(models.Model):
    TYPES = [("asset", "Asset"), ("liability", "Liability"), ("revenue", "Revenue")]

    code = models.CharField(max_length=30)
    name = models.CharField(max_length=80)
    type = models.CharField(max_length=10, choices=TYPES)
    merchant = models.ForeignKey(Merchant, null=True, blank=True, on_delete=models.CASCADE, related_name="ledger_accounts")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["code", "merchant"], name="uniq_account_code_merchant")]
        ordering = ["type", "code"]

    def __str__(self):
        return self.name


class Settlement(models.Model):
    STATUS = [("processed", "Processed")]
    public_id = models.CharField(max_length=32, unique=True, default=stl_id, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="settlements")
    gross = models.BigIntegerField(default=0)
    fees = models.BigIntegerField(default=0, help_text="Gateway fee + GST")
    refunds_total = models.BigIntegerField(default=0)
    net = models.BigIntegerField(default=0)
    payments_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, default="processed")
    utr = models.CharField(max_length=24, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class LedgerTransaction(models.Model):
    """One business event; its entries must always balance (sum debits == sum credits)."""

    KINDS = [("capture", "Capture"), ("refund", "Refund"), ("settlement", "Settlement")]
    public_id = models.CharField(max_length=32, unique=True, default=txn_id, editable=False)
    kind = models.CharField(max_length=12, choices=KINDS)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name="ledger_transactions")
    payment = models.ForeignKey("payments.Payment", null=True, blank=True, on_delete=models.PROTECT, related_name="ledger_txns")
    refund = models.ForeignKey("payments.Refund", null=True, blank=True, on_delete=models.PROTECT)
    settlement = models.ForeignKey(Settlement, null=True, blank=True, on_delete=models.PROTECT)
    description = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class LedgerEntry(models.Model):
    DIRECTIONS = [("D", "Debit"), ("C", "Credit")]
    transaction = models.ForeignKey(LedgerTransaction, on_delete=models.CASCADE, related_name="entries")
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name="entries")
    direction = models.CharField(max_length=1, choices=DIRECTIONS)
    amount = models.BigIntegerField()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="entry_amount_positive")]
        indexes = [models.Index(fields=["account", "direction"])]
