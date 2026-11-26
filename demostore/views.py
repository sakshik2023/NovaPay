import json

from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.utils import hmac_sha256, safe_equal
from webhooks.services import verify_signature
from . import gateway_client as gw
from .models import ReceivedWebhook, StoreOrder

PRODUCTS = [
    {"id": "py", "emoji": "🐍", "name": "Python Mastery Course", "desc": "40 hours of hands-on Python, from basics to OOP.", "price": 149900},
    {"id": "dj", "emoji": "🎸", "name": "Django Weekend Workshop", "desc": "Build and deploy a full-stack Django app in 2 days.", "price": 99900},
    {"id": "ds", "emoji": "📊", "name": "Data Science Bundle", "desc": "Pandas, ML basics, and 5 portfolio projects.", "price": 499900},
    {"id": "cv", "emoji": "📝", "name": "Resume Review", "desc": "Detailed written feedback within 48 hours.", "price": 49900},
    {"id": "mt", "emoji": "🧭", "name": "1:1 Mentorship (1 month)", "desc": "Four live sessions with an industry mentor.", "price": 249900},
    {"id": "ex", "emoji": "🎓", "name": "Certification Exam", "desc": "Proctored exam + digital certificate.", "price": 79900},
]
BY_ID = {p["id"]: p for p in PRODUCTS}


def _cart(request):
    return request.session.setdefault("cart", {})


def _lines(request):
    lines, total = [], 0
    for pid, qty in _cart(request).items():
        p = BY_ID.get(pid)
        if p:
            lines.append({**p, "qty": qty, "line": p["price"] * qty})
            total += p["price"] * qty
    return lines, total


def index(request):
    return render(request, "demostore/index.html", {"products": PRODUCTS, "cart_count": sum(_cart(request).values()),
                                                    "creds": gw.credentials() is not None})


@require_POST
def add(request, pid):
    if pid in BY_ID:
        cart = _cart(request)
        cart[pid] = cart.get(pid, 0) + 1
        request.session.modified = True
        messages.success(request, f"Added {BY_ID[pid]['name']} to cart")
    return redirect("store_index")


def cart(request):
    lines, total = _lines(request)
    return render(request, "demostore/cart.html", {"lines": lines, "total": total, "cart_count": sum(_cart(request).values())})


@require_POST
def remove(request, pid):
    _cart(request).pop(pid, None)
    request.session.modified = True
    return redirect("store_cart")


@require_POST
def checkout(request):
    lines, total = _lines(request)
    if not lines:
        return redirect("store_index")
    so = StoreOrder.objects.create(total_paise=total, items=[{"name": l["name"], "qty": l["qty"], "line": l["line"]} for l in lines])
    callback = request.build_absolute_uri("/demo-store/return/")
    try:
        status, body = gw.create_order(total, so.public_id, ", ".join(l["name"] for l in lines)[:190], callback)
    except Exception as e:  # gateway unreachable / credentials missing
        so.delete()
        messages.error(request, f"Could not reach the payment gateway: {e}")
        return redirect("store_cart")
    if status != 201:
        so.delete()
        messages.error(request, f"Gateway error: {body.get('error', {}).get('description', status)}")
        return redirect("store_cart")
    so.gateway_order_id = body["id"]
    so.save(update_fields=["gateway_order_id"])
    request.session["cart"] = {}
    return redirect(body["checkout_url"])


def payment_return(request):
    """Customer is redirected here by the gateway. NEVER trust the query string alone:
    1) verify the HMAC signature, 2) confirm the status server-to-server."""
    g = request.GET
    creds = gw.credentials() or {}
    order_id, payment_id, status, sig = g.get("order_id", ""), g.get("payment_id", ""), g.get("status", ""), g.get("signature", "")
    expected = hmac_sha256(creds.get("webhook_secret", ""), f"{order_id}|{payment_id}")
    sig_ok = bool(sig) and safe_equal(sig, expected)
    so = StoreOrder.objects.filter(gateway_order_id=order_id).first()
    api_status = None
    if sig_ok and so:
        code, body = gw.fetch_payment(payment_id)
        api_status = body.get("status") if code == 200 else None
        if api_status == "captured" and so.status != "paid":
            so.status, so.gateway_payment_id = "paid", payment_id
            so.save(update_fields=["status", "gateway_payment_id"])
        elif api_status == "failed" and so.status == "awaiting_payment":
            so.status, so.gateway_payment_id = "payment_failed", payment_id
            so.save(update_fields=["status", "gateway_payment_id"])
    return render(request, "demostore/return.html", {"so": so, "sig_ok": sig_ok, "api_status": api_status,
                                                     "payment_id": payment_id, "claimed": status})


@csrf_exempt
@require_POST
def webhook(request):
    """The merchant's webhook receiver: verify signature, then act."""
    body = request.body.decode()
    secret = (gw.credentials() or {}).get("webhook_secret", "")
    valid = verify_signature(secret, body, request.META.get("HTTP_X_NOVAPAY_SIGNATURE", ""))
    try:
        evt = json.loads(body)
    except ValueError:
        return HttpResponseBadRequest("bad json")
    ReceivedWebhook.objects.create(event_id=evt.get("id", ""), event_type=evt.get("type", ""), signature_valid=valid, body=body)
    if not valid:
        return HttpResponse("invalid signature", status=400)
    if evt.get("type") == "payment.captured":
        data = evt["data"]
        so = StoreOrder.objects.filter(gateway_order_id=data.get("order_id")).first()
        if so and so.status != "paid":
            so.status, so.gateway_payment_id = "paid", data["id"]
            so.save(update_fields=["status", "gateway_payment_id"])
    return HttpResponse("ok")


def orders(request):
    return render(request, "demostore/orders.html", {"orders": StoreOrder.objects.all()[:20],
                                                     "hooks": ReceivedWebhook.objects.all()[:15]})
