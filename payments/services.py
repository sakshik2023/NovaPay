"""Business logic for orders, payments and refunds.

Concurrency model: every critical state change is a compare-and-swap `UPDATE ... WHERE status = <expected>`
(single atomic statement). Works on SQLite and PostgreSQL, so double-capture / over-refund cannot happen
even with parallel requests.
"""
from django.conf import settings
from django.db import transaction
from django.db.models import Case, F, Value, When
from django.utils import timezone

from core.exceptions import GatewayError
from core.models import audit
from core.utils import hmac_sha256, pct, sha256
from ledger import services as ledger
from webhooks import services as hooks
from . import fraud, simulator
from .models import Order, Payment, PaymentEvent, Refund
from .serializers import order_dict, payment_dict, refund_dict


# ----------------------------------------------------------------- orders
def create_order(merchant, amount, currency="INR", receipt="", description="", notes=None,
                 callback_url="", customer_name="", customer_email="", source="api", actor="api", ip=None):
    if currency != "INR":
        raise GatewayError("Only INR is supported", field="currency")
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise GatewayError("Amount must be an integer in paise", field="amount")
    if amount < settings.MIN_AMOUNT_PAISE:
        raise GatewayError(f"Amount must be at least {settings.MIN_AMOUNT_PAISE} paise (Rs 1)", field="amount")
    if amount > settings.MAX_AMOUNT_PAISE:
        raise GatewayError("Amount exceeds the maximum allowed per order", field="amount")
    order = Order.objects.create(
        merchant=merchant, amount=amount, currency=currency, receipt=receipt, description=description,
        notes=notes or {}, callback_url=callback_url, customer_name=customer_name,
        customer_email=customer_email, source=source)
    audit(merchant, actor, "order.created", order, ip=ip, amount=amount)
    return order


def expire_stale_orders():
    return Order.objects.filter(status__in=["created", "attempted"], expires_at__lt=timezone.now()).update(status="expired")


# --------------------------------------------------------------- payments
def _event(payment, kind, message):
    PaymentEvent.objects.create(payment=payment, kind=kind, message=message)


def callback_signature(order, payment):
    return hmac_sha256(order.merchant.webhook_secret, f"{order.public_id}|{payment.public_id}")


def _validate_instrument(method, data):
    if method == "card":
        number = simulator.normalise_card(data.get("card_number", ""))
        ok, field, msg = simulator.validate_card(number, data.get("expiry", ""), data.get("cvv", ""))
        if not ok:
            raise GatewayError(msg, field=field)
        return {"card_number": number}
    if method == "upi":
        vpa = (data.get("vpa") or "").strip()
        if not simulator.validate_vpa(vpa):
            raise GatewayError("Enter a valid UPI ID like name@bank", field="vpa")
        return {"vpa": vpa}
    raise GatewayError("Unsupported payment method", field="method")


def start_payment(order, method, data, ip=None):
    """Create a payment attempt for an order and run it through fraud checks and the bank simulator."""
    if order.is_expired:
        Order.objects.filter(pk=order.pk).exclude(status="paid").update(status="expired")
        raise GatewayError("This payment link has expired", code="ORDER_EXPIRED", status_code=410)
    if order.status == "paid":
        raise GatewayError("This order has already been paid", code="ORDER_ALREADY_PAID", status_code=409)

    inst = _validate_instrument(method, data)
    raw_card = inst.get("card_number", "")
    fingerprint = sha256(settings.CARD_FINGERPRINT_SALT + raw_card) if raw_card else ""
    verdict = fraud.evaluate(order.merchant, order.amount, ip, fingerprint, raw_card)

    fee = pct(order.amount, settings.FEE_PERCENT)
    gst = pct(fee, settings.GST_PERCENT)
    with transaction.atomic():
        payment = Payment.objects.create(
            order=order, merchant=order.merchant, amount=order.amount, method=method,
            card_last4=raw_card[-4:] if raw_card else "",
            card_network=simulator.detect_network(raw_card) if raw_card else "",
            card_fingerprint=fingerprint, upi_vpa=inst.get("vpa", ""),
            customer_email=data.get("email", "") or order.customer_email,
            fee=fee, gst=gst, net_amount=order.amount - fee - gst,
            fraud_score=verdict.score, fraud_decision=verdict.decision, fraud_reasons=verdict.reasons, ip=ip)
        Order.objects.filter(pk=order.pk, status="created").update(status="attempted")
        _event(payment, "created", f"Payment attempt started via {method.upper()}")
        if verdict.decision != "allow":
            _event(payment, "fraud", f"Fraud engine: {verdict.decision.upper()} (score {verdict.score}) — "
                                     + "; ".join(verdict.reasons))

    if verdict.decision == "block":
        fail(payment, "FRAUD_BLOCKED", "Payment blocked by risk checks")
        return payment

    result = simulator.charge_card(raw_card) if method == "card" else simulator.charge_upi(inst["vpa"])
    if result.status == "success":
        capture(payment, result.reference)
    elif result.status == "otp_required":
        Payment.objects.filter(pk=payment.pk).update(bank_reference=result.reference)
        _event(payment, "otp_required", "3-D Secure: OTP sent to the cardholder")
    else:
        fail(payment, result.code, result.reason)
    payment.refresh_from_db()
    return payment


def confirm_otp(payment, otp):
    if payment.status != "pending":
        raise GatewayError("This payment is no longer awaiting OTP", code="INVALID_STATE", status_code=409)
    Payment.objects.filter(pk=payment.pk).update(otp_attempts=F("otp_attempts") + 1)
    payment.refresh_from_db()
    if simulator.verify_otp(otp):
        capture(payment, payment.bank_reference)
    elif payment.otp_attempts >= 3:
        fail(payment, "OTP_ATTEMPTS_EXCEEDED", "Too many wrong OTP attempts")
    else:
        _event(payment, "otp_failed", f"Wrong OTP entered (attempt {payment.otp_attempts}/3)")
        raise GatewayError(f"Incorrect OTP. {3 - payment.otp_attempts} attempt(s) left", code="INVALID_OTP", field="otp")
    payment.refresh_from_db()
    return payment


def capture(payment, bank_reference=""):
    """pending -> captured. Writes ledger and emits webhooks atomically."""
    with transaction.atomic():
        won = Payment.objects.filter(pk=payment.pk, status="pending").update(
            status="captured", captured_at=timezone.now(), bank_reference=bank_reference or payment.bank_reference)
        if not won:
            return payment
        # Order may be paid by only ONE payment (prevents double charge on parallel attempts)
        paid = Order.objects.filter(pk=payment.order_id, status__in=["created", "attempted"]).update(status="paid")
        if not paid:
            Payment.objects.filter(pk=payment.pk).update(status="failed", captured_at=None,
                                                         failure_code="ORDER_ALREADY_PAID",
                                                         failure_reason="Order was already paid by another attempt")
            payment.refresh_from_db()
            _event(payment, "failed", "Order was already paid by another attempt")
            return payment
        payment.refresh_from_db()
        ledger.post_capture(payment)
        _event(payment, "captured", f"Payment captured. Bank ref {payment.bank_reference}")
        hooks.emit(payment.merchant, "payment.captured", payment_dict(payment))
        hooks.emit(payment.merchant, "order.paid", order_dict(payment.order))
        audit(payment.merchant, "system", "payment.captured", payment, amount=payment.amount)
    return payment


def fail(payment, code, reason):
    with transaction.atomic():
        won = Payment.objects.filter(pk=payment.pk, status="pending").update(
            status="failed", failure_code=code, failure_reason=reason)
        if not won:
            return payment
        payment.refresh_from_db()
        _event(payment, "failed", f"Payment failed: {reason}")
        hooks.emit(payment.merchant, "payment.failed", payment_dict(payment))
    return payment


# ----------------------------------------------------------------- refunds
def refund_payment(payment, amount=None, reason="", actor="api", ip=None):
    """Full or partial refund. The over-refund guard is one atomic UPDATE, safe under parallel requests."""
    with transaction.atomic():
        payment.refresh_from_db()
        if payment.status not in ("captured", "partially_refunded"):
            raise GatewayError(f"Payment in status '{payment.status}' cannot be refunded",
                               code="INVALID_STATE", status_code=409)
        amount = payment.refundable if amount is None else amount
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise GatewayError("Refund amount must be a positive integer in paise", field="amount")
        ok = Payment.objects.filter(
            pk=payment.pk, status__in=["captured", "partially_refunded"],
            refunded_amount__lte=F("amount") - amount).update(
            refunded_amount=F("refunded_amount") + amount,
            # SET clauses see the OLD row: old refunded == amount - refund  =>  fully refunded after this update
            status=Case(When(refunded_amount=F("amount") - amount, then=Value("refunded")),
                        default=Value("partially_refunded")))
        if not ok:
            payment.refresh_from_db()
            raise GatewayError(f"Refund exceeds refundable amount ({payment.refundable} paise left)",
                               field="amount", code="REFUND_EXCEEDS_AMOUNT")
        payment.refresh_from_db()
        refund = Refund.objects.create(payment=payment, merchant=payment.merchant, amount=amount, reason=reason)
        ledger.post_refund(refund)
        _event(payment, "refund", f"Refunded Rs {amount / 100:.2f}" + (f" — {reason}" if reason else ""))
        hooks.emit(payment.merchant, "refund.processed", refund_dict(refund))
        audit(payment.merchant, actor, "refund.created", refund, ip=ip, amount=amount)
    return refund
