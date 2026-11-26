"""Public REST API (v1). Auth: HTTP Basic with API key id/secret. Write endpoints accept Idempotency-Key."""
from urllib.parse import urlparse

from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from core.exceptions import GatewayError
from core.idempotency import run_idempotent
from core.models import audit
from core.utils import client_ip
from ledger.models import Settlement
from webhooks import services as hooks
from . import services
from .models import Order, Payment
from .serializers import order_dict, payment_dict, refund_dict


class GatewayAPIView(APIView):
    def finalize_response(self, request, response, *a, **kw):
        response = super().finalize_response(request, response, *a, **kw)
        rl = getattr(request, "rate_limit", None)
        if rl:
            response["X-RateLimit-Limit"] = str(rl["limit"])
            response["X-RateLimit-Remaining"] = str(rl["remaining"])
        return response

    @property
    def merchant(self):
        return self.request.merchant


def _paginate(request, qs, to_dict):
    try:
        limit = max(1, min(int(request.query_params.get("count", 25)), 100))
    except ValueError:
        raise GatewayError("count must be an integer", field="count")
    after = request.query_params.get("starting_after")
    if after:
        anchor = qs.model.objects.filter(public_id=after).first()
        if anchor:
            qs = qs.filter(id__lt=anchor.id)
    rows = list(qs.order_by("-id")[: limit + 1])
    return {"entity": "list", "count": min(len(rows), limit), "has_more": len(rows) > limit,
            "items": [to_dict(r) for r in rows[:limit]]}


def _validate_url(url, field):
    if url and urlparse(url).scheme not in ("http", "https"):
        raise GatewayError("Must be an http(s) URL", field=field)


class OrderList(GatewayAPIView):
    def get(self, request):
        qs = Order.objects.filter(merchant=self.merchant)
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        return Response(_paginate(request, qs, order_dict))

    def post(self, request):
        def handler():
            d = request.data
            if "amount" not in d:
                raise GatewayError("amount is required", field="amount")
            _validate_url(d.get("callback_url", ""), "callback_url")
            notes = d.get("notes") or {}
            if not isinstance(notes, dict):
                raise GatewayError("notes must be an object", field="notes")
            order = services.create_order(
                self.merchant, d["amount"], d.get("currency", "INR"), str(d.get("receipt", ""))[:60],
                str(d.get("description", ""))[:200], notes, d.get("callback_url", ""),
                customer_name=str(d.get("customer_name", ""))[:80], customer_email=d.get("customer_email", ""),
                actor="api", ip=client_ip(request))
            return Response(order_dict(order), status=201)
        return run_idempotent(request, "POST /orders", handler)


class OrderDetail(GatewayAPIView):
    def get(self, request, public_id):
        order = get_object_or_404(Order, public_id=public_id, merchant=self.merchant)
        data = order_dict(order)
        data["payments"] = [payment_dict(p) for p in order.payments.all()]
        return Response(data)


class PaymentList(GatewayAPIView):
    def get(self, request):
        qs = Payment.objects.filter(merchant=self.merchant).select_related("order")
        q = request.query_params
        for f in ("status", "method"):
            if q.get(f):
                qs = qs.filter(**{f: q[f]})
        if q.get("order_id"):
            qs = qs.filter(order__public_id=q["order_id"])
        return Response(_paginate(request, qs, payment_dict))

    def post(self, request):
        """Server-to-server payment creation (test mode). Real card data should only touch the hosted checkout."""
        def handler():
            d = request.data
            order = get_object_or_404(Order, public_id=d.get("order_id", ""), merchant=self.merchant)
            payment = services.start_payment(order, d.get("method", ""), d, ip=client_ip(request))
            body = payment_dict(payment)
            if payment.status == "pending":
                body["next_action"] = {"type": "otp", "url": f"/v1/payments/{payment.public_id}/otp"}
            return Response(body, status=201)
        return run_idempotent(request, "POST /payments", handler)


class PaymentDetail(GatewayAPIView):
    def get(self, request, public_id):
        p = get_object_or_404(Payment.objects.select_related("order"), public_id=public_id, merchant=self.merchant)
        return Response(payment_dict(p))


class PaymentOTP(GatewayAPIView):
    def post(self, request, public_id):
        p = get_object_or_404(Payment.objects.select_related("order"), public_id=public_id, merchant=self.merchant)
        p = services.confirm_otp(p, str(request.data.get("otp", "")))
        return Response(payment_dict(p))


class RefundList(GatewayAPIView):
    def get(self, request, public_id):
        p = get_object_or_404(Payment, public_id=public_id, merchant=self.merchant)
        return Response({"entity": "list", "items": [refund_dict(r) for r in p.refunds.all()]})

    def post(self, request, public_id):
        def handler():
            p = get_object_or_404(Payment.objects.select_related("order"), public_id=public_id, merchant=self.merchant)
            r = services.refund_payment(p, request.data.get("amount"), str(request.data.get("reason", ""))[:200],
                                        actor="api", ip=client_ip(request))
            return Response(refund_dict(r), status=201)
        return run_idempotent(request, f"POST /payments/{public_id}/refunds", handler)


class SettlementList(GatewayAPIView):
    def get(self, request):
        def d(s):
            return {"id": s.public_id, "entity": "settlement", "gross": s.gross, "fees": s.fees,
                    "refunds": s.refunds_total, "net": s.net, "utr": s.utr, "status": s.status,
                    "created_at": int(s.created_at.timestamp())}
        return Response(_paginate(request, Settlement.objects.filter(merchant=self.merchant), d))


class WebhookTest(GatewayAPIView):
    def post(self, request):
        if not self.merchant.webhook_url:
            raise GatewayError("Set a webhook URL in the dashboard first", code="WEBHOOK_NOT_CONFIGURED")
        ev = hooks.emit(self.merchant, "webhook.test", {"message": "Hello from NovaPay"})
        audit(self.merchant, "api", "webhook.test", ip=client_ip(request))
        return Response({"event_id": ev.public_id, "queued": True}, status=202)


class Ping(GatewayAPIView):
    def get(self, request):
        return Response({"ok": True, "merchant": self.merchant.business_name})
