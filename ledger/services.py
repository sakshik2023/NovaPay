"""Double-entry ledger.  Every money movement is a LedgerTransaction whose entries sum to zero
(sum of debits == sum of credits).  Amounts are integer paise."""
import secrets

from django.db import transaction
from django.db.models import Q, Sum

from core.exceptions import GatewayError
from .models import LedgerAccount, LedgerEntry, LedgerTransaction, Settlement

BANK = ("BANK", "Bank Settlement Account", "asset")
FEE = ("FEE_REVENUE", "Gateway Fee Revenue", "revenue")
GST = ("GST_PAYABLE", "GST Payable", "liability")
PAYABLE = ("MERCHANT_PAYABLE", "Merchant Payable", "liability")


def account(spec, merchant=None):
    code, name, typ = spec
    acc, _ = LedgerAccount.objects.get_or_create(code=code, merchant=merchant,
                                                 defaults={"name": name, "type": typ})
    return acc


def _post(kind, merchant, lines, description, **links):
    """lines = [(account, 'D'|'C', amount)]; refuses unbalanced postings."""
    debit = sum(a for _, d, a in lines if d == "D")
    credit = sum(a for _, d, a in lines if d == "C")
    if debit != credit:
        raise GatewayError(f"Unbalanced ledger posting ({debit} != {credit})", code="LEDGER_ERROR", status_code=500)
    txn = LedgerTransaction.objects.create(kind=kind, merchant=merchant, description=description, **links)
    LedgerEntry.objects.bulk_create(
        [LedgerEntry(transaction=txn, account=acc, direction=d, amount=a) for acc, d, a in lines if a > 0]
    )
    return txn


def post_capture(payment):
    m = payment.merchant
    return _post("capture", m, [
        (account(BANK), "D", payment.amount),
        (account(PAYABLE, m), "C", payment.net_amount),
        (account(FEE), "C", payment.fee),
        (account(GST), "C", payment.gst),
    ], f"Capture {payment.public_id}", payment=payment)


def post_refund(refund):
    m = refund.merchant
    return _post("refund", m, [
        (account(PAYABLE, m), "D", refund.amount),
        (account(BANK), "C", refund.amount),
    ], f"Refund {refund.public_id}", payment=refund.payment, refund=refund)


def balance(acc):
    d = acc.entries.filter(direction="D").aggregate(s=Sum("amount"))["s"] or 0
    c = acc.entries.filter(direction="C").aggregate(s=Sum("amount"))["s"] or 0
    return d - c if acc.type == "asset" else c - d


def merchant_payable(merchant):
    return balance(account(PAYABLE, merchant))


def trial_balance(merchant=None):
    """Returns (rows, total_debit, total_credit). Debits must equal credits for the whole ledger."""
    qs = LedgerEntry.objects.all()
    if merchant is not None:
        qs = qs.filter(transaction__merchant=merchant)
    rows = []
    for acc in LedgerAccount.objects.filter(entries__in=qs).distinct():
        e = qs.filter(account=acc)
        d = e.filter(direction="D").aggregate(s=Sum("amount"))["s"] or 0
        c = e.filter(direction="C").aggregate(s=Sum("amount"))["s"] or 0
        rows.append({"account": acc, "debit": d, "credit": c})
    td = sum(r["debit"] for r in rows)
    tc = sum(r["credit"] for r in rows)
    return rows, td, tc


def verify_ledger():
    """Global invariants: every transaction balances, and total debits == total credits."""
    bad = []
    for t in LedgerTransaction.objects.all():
        s = t.entries.aggregate(
            d=Sum("amount", filter=Q(direction="D")), c=Sum("amount", filter=Q(direction="C")))
        if (s["d"] or 0) != (s["c"] or 0):
            bad.append(t.public_id)
    return bad


@transaction.atomic
def run_settlement(merchant):
    """Pay out everything captured/refunded since the last settlement.  Returns Settlement or None."""
    from payments.models import Payment, Refund

    payments = list(Payment.objects.select_for_update().filter(
        merchant=merchant, settlement__isnull=True,
        status__in=["captured", "partially_refunded", "refunded"]))
    refunds = list(Refund.objects.select_for_update().filter(merchant=merchant, settlement__isnull=True))
    # A refund may belong to an older, already-settled payment: still deducted from this payout.
    if not payments and not refunds:
        return None
    gross = sum(p.amount for p in payments)
    fees = sum(p.fee + p.gst for p in payments)
    refunded = sum(r.amount for r in refunds)
    net = gross - fees - refunded
    if net <= 0:
        return None
    stl = Settlement.objects.create(
        merchant=merchant, gross=gross, fees=fees, refunds_total=refunded, net=net,
        payments_count=len(payments), utr="UTR" + secrets.token_hex(6).upper())
    _post("settlement", merchant, [
        (account(PAYABLE, merchant), "D", net),
        (account(BANK), "C", net),
    ], f"Settlement {stl.public_id}", settlement=stl)
    Payment.objects.filter(pk__in=[p.pk for p in payments]).update(settlement=stl)
    Refund.objects.filter(pk__in=[r.pk for r in refunds]).update(settlement=stl)
    return stl
