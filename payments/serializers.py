"""Plain dict renderers shared by the REST API and webhook payloads (single source of truth)."""
from django.utils import timezone


def ts(dt):
    return int(dt.timestamp()) if dt else None


def order_dict(o):
    return {
        "id": o.public_id, "entity": "order", "amount": o.amount, "currency": o.currency,
        "status": o.status, "receipt": o.receipt, "description": o.description, "notes": o.notes,
        "checkout_url": f"/pay/{o.public_id}/", "created_at": ts(o.created_at), "expires_at": ts(o.expires_at),
    }


def payment_dict(p):
    return {
        "id": p.public_id, "entity": "payment", "order_id": p.order.public_id, "amount": p.amount,
        "currency": p.order.currency, "status": p.status, "method": p.method,
        "card": {"network": p.card_network, "last4": p.card_last4} if p.method == "card" else None,
        "vpa": p.upi_vpa or None, "fee": p.fee, "gst": p.gst, "net_amount": p.net_amount,
        "amount_refunded": p.refunded_amount, "error_code": p.failure_code or None,
        "error_description": p.failure_reason or None,
        "fraud": {"score": p.fraud_score, "decision": p.fraud_decision},
        "created_at": ts(p.created_at), "captured_at": ts(p.captured_at),
    }


def refund_dict(r):
    return {"id": r.public_id, "entity": "refund", "payment_id": r.payment.public_id, "amount": r.amount,
            "status": r.status, "reason": r.reason, "created_at": ts(r.created_at)}
