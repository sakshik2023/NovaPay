import json
from urllib.parse import urlencode

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from core.exceptions import GatewayError
from core.utils import client_ip
from payments import services, simulator
from payments.models import Order, Payment

TEST_CARDS = [
    ("4111 1111 1111 1111", "Visa", "Success", "ok"),
    ("5555 5555 5555 4444", "Mastercard", "Success", "ok"),
    ("4000 0000 0000 3220", "Visa", "3-D Secure · OTP 123456", "warn"),
    ("4000 0000 0000 9995", "Visa", "Insufficient funds", "bad"),
    ("4000 0000 0000 0069", "Visa", "Card expired", "bad"),
    ("4000 0000 0000 0119", "Visa", "Blocked by fraud engine", "bad"),
]
TEST_UPI = [("success@upi", "Success", "ok"), ("failure@upi", "Declined", "bad"), ("timeout@upi", "Bank timeout", "bad")]


def _return_url(order, payment):
    if not order.callback_url:
        return ""
    q = urlencode({"order_id": order.public_id, "payment_id": payment.public_id, "status": payment.status,
                   "signature": services.callback_signature(order, payment)})
    sep = "&" if "?" in order.callback_url else "?"
    return f"{order.callback_url}{sep}{q}"


def _payload(payment):
    order = payment.order
    return {"payment_id": payment.public_id, "status": payment.status, "amount": payment.amount,
            "error_code": payment.failure_code, "error": payment.failure_reason,
            "needs_otp": payment.status == "pending", "return_url": _return_url(order, payment)}


@ensure_csrf_cookie
def checkout_page(request, order_id):
    order = get_object_or_404(Order.objects.select_related("merchant"), public_id=order_id)
    state = "pay"
    if order.status == "paid":
        state = "paid"
    elif order.is_expired:
        state = "expired"
    paid = order.payments.filter(status__in=["captured", "partially_refunded", "refunded"]).first()
    return render(request, "checkout/checkout.html", {
        "order": order, "m": order.merchant, "state": state, "paid_payment": paid,
        "test_cards": TEST_CARDS, "test_upi": TEST_UPI,
        "return_url": _return_url(order, paid) if paid else ""})


def _json_body(request):
    try:
        return json.loads(request.body or "{}")
    except ValueError:
        raise GatewayError("Invalid JSON body")


@require_POST
def submit(request, order_id):
    order = get_object_or_404(Order.objects.select_related("merchant"), public_id=order_id)
    try:
        data = _json_body(request)
        payment = services.start_payment(order, data.get("method", ""), data, ip=client_ip(request))
    except GatewayError as e:
        return JsonResponse({"error": {"code": e.code, "field": e.field, "description": e.description}},
                            status=e.status_code)
    return JsonResponse(_payload(payment))


@require_POST
def otp(request, order_id):
    order = get_object_or_404(Order, public_id=order_id)
    try:
        data = _json_body(request)
        payment = get_object_or_404(Payment.objects.select_related("order__merchant"),
                                    public_id=data.get("payment_id", ""), order=order)
        payment = services.confirm_otp(payment, str(data.get("otp", "")))
    except GatewayError as e:
        return JsonResponse({"error": {"code": e.code, "field": e.field, "description": e.description}},
                            status=e.status_code)
    return JsonResponse(_payload(payment))
