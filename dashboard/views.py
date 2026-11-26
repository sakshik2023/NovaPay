import csv
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.exceptions import GatewayError
from core.models import APIKey, AuditLog, Merchant, audit
from core.utils import client_ip, format_inr
from ledger import services as ledger
from ledger.models import LedgerTransaction, Settlement
from payments import services
from payments.models import Order, Payment, Refund
from webhooks import services as hooks
from webhooks.models import WebhookDelivery, WebhookEvent

SUCCESS = ["captured", "partially_refunded", "refunded"]


def merchant_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *a, **kw):
        m = getattr(request.user, "merchant", None)
        if m is None:
            messages.error(request, "This account has no merchant profile.")
            return redirect("/admin/")
        request.merchant = m
        return view(request, *a, **kw)
    return wrapper


def to_paise(value):
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        raise GatewayError("Enter a valid amount", field="amount")
    if d <= 0 or d != d.quantize(Decimal("0.01")):
        raise GatewayError("Amount must be positive with at most 2 decimals", field="amount")
    return int(d * 100)


def ctx(request, nav, **extra):
    m = request.merchant
    return {"nav": nav, "merchant": m,
            "flagged_count": Payment.objects.filter(merchant=m, fraud_decision__in=["review", "block"],
                                                    created_at__gte=timezone.now() - timedelta(days=7)).count(), **extra}


# ------------------------------------------------------------------ public
def landing(request):
    if request.user.is_authenticated and hasattr(request.user, "merchant"):
        return redirect("overview")
    return render(request, "landing.html")


def api_docs(request):
    return render(request, "docs.html")


def register(request):
    if request.method == "POST":
        name = request.POST.get("business_name", "").strip()
        email = request.POST.get("email", "").strip().lower()
        pw = request.POST.get("password", "")
        errors = {}
        if len(name) < 2:
            errors["business_name"] = "Enter your business name"
        if "@" not in email:
            errors["email"] = "Enter a valid email"
        elif User.objects.filter(username=email).exists():
            errors["email"] = "An account with this email already exists"
        if len(pw) < 8:
            errors["password"] = "Password must be at least 8 characters"
        if not errors:
            user = User.objects.create_user(username=email, email=email, password=pw)
            m = Merchant.objects.create(user=user, business_name=name,
                                        business_type=request.POST.get("business_type", "Retail"))
            key, secret = APIKey.generate(m, "Default key")
            audit(m, email, "merchant.registered", m, ip=client_ip(request))
            login(request, user)
            request.session["new_key"] = {"key_id": key.key_id, "secret": secret}
            messages.success(request, f"Welcome to NovaPay, {name}! Your test API key is ready.")
            return redirect("keys")
        return render(request, "registration/register.html", {"errors": errors, "form": request.POST})
    return render(request, "registration/register.html", {"errors": {}, "form": {}})


# ---------------------------------------------------------------- overview
@merchant_required
def overview(request):
    m = request.merchant
    now = timezone.now()
    since = now - timedelta(days=30)
    base = Payment.objects.filter(merchant=m)
    month = base.filter(created_at__gte=since)
    ok = month.filter(status__in=SUCCESS)
    volume = ok.aggregate(s=Sum("amount"))["s"] or 0
    n_ok = ok.count()
    n_fail = month.filter(status="failed").count()
    attempts = n_ok + n_fail
    success_rate = round(100 * n_ok / attempts) if attempts else 0
    refunded = Refund.objects.filter(merchant=m, created_at__gte=since).aggregate(s=Sum("amount"))["s"] or 0

    # 14-day volume bars
    days = [(now - timedelta(days=i)).astimezone(timezone.get_current_timezone()).date() for i in range(13, -1, -1)]
    by_day = {r["d"]: r["s"] for r in ok.filter(created_at__gte=now - timedelta(days=14))
              .annotate(d=TruncDate("created_at")).values("d").annotate(s=Sum("amount"))}
    mx = max([by_day.get(d, 0) for d in days] + [1])
    bars = [{"label": d.strftime("%d %b"), "short": d.strftime("%d"), "value": by_day.get(d, 0),
             "pct": max(2, round(100 * by_day.get(d, 0) / mx)) if by_day.get(d) else 2} for d in days]

    # method split
    split = {r["method"]: r["c"] for r in ok.values("method").annotate(c=Count("id"))}
    card, upi = split.get("card", 0), split.get("upi", 0)
    tot = card + upi
    card_pct = round(100 * card / tot) if tot else 0

    return render(request, "dashboard/overview.html", ctx(
        request, "overview", volume=volume, n_ok=n_ok, n_fail=n_fail, success_rate=success_rate,
        refunded=refunded, avg_ticket=(volume // n_ok) if n_ok else 0, bars=bars, card=card, upi=upi,
        card_pct=card_pct, upi_pct=100 - card_pct if tot else 0, split_total=tot,
        payable=ledger.merchant_payable(m), recent=base.select_related("order")[:8],
        flagged=month.filter(fraud_decision__in=["review", "block"]).count(),
        new_key=request.session.get("new_key")))


# ---------------------------------------------------------------- payments
def _filtered_payments(request):
    m = request.merchant
    qs = Payment.objects.filter(merchant=m).select_related("order")
    g = request.GET
    if g.get("q"):
        q = g["q"].strip()
        qs = qs.filter(Q(public_id__icontains=q) | Q(order__public_id__icontains=q) | Q(customer_email__icontains=q)
                       | Q(card_last4=q) | Q(upi_vpa__icontains=q) | Q(order__receipt__icontains=q))
    if g.get("status"):
        qs = qs.filter(status=g["status"])
    if g.get("method"):
        qs = qs.filter(method=g["method"])
    if g.get("risk"):
        qs = qs.filter(fraud_decision=g["risk"])
    if g.get("from"):
        qs = qs.filter(created_at__date__gte=g["from"])
    if g.get("to"):
        qs = qs.filter(created_at__date__lte=g["to"])
    return qs


@merchant_required
def payments_list(request):
    qs = _filtered_payments(request)
    if request.GET.get("export") == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="novapay_payments.csv"'
        w = csv.writer(resp)
        w.writerow(["payment_id", "order_id", "created_at", "status", "method", "instrument", "amount_inr",
                    "fee_inr", "gst_inr", "net_inr", "refunded_inr", "fraud_score", "failure_reason"])
        for p in qs.iterator():
            w.writerow([p.public_id, p.order.public_id, p.created_at.isoformat(timespec="seconds"), p.status, p.method,
                        p.instrument, p.amount / 100, p.fee / 100, p.gst / 100, p.net_amount / 100,
                        p.refunded_amount / 100, p.fraud_score, p.failure_reason])
        audit(request.merchant, request.user.username, "payments.exported", ip=client_ip(request))
        return resp
    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    totals = qs.filter(status__in=SUCCESS).aggregate(s=Sum("amount"), n=Count("id"))
    return render(request, "dashboard/payments.html", ctx(request, "payments", payments=page, totals=totals,
                                                          statuses=Payment.STATUS))


@merchant_required
def payment_detail(request, public_id):
    p = get_object_or_404(Payment.objects.select_related("order"), public_id=public_id, merchant=request.merchant)
    if request.method == "POST":
        try:
            raw = request.POST.get("amount", "").strip()
            amount = to_paise(raw) if raw else None
            services.refund_payment(p, amount, request.POST.get("reason", "")[:200],
                                    actor=request.user.username, ip=client_ip(request))
            messages.success(request, "Refund processed.")
        except GatewayError as e:
            messages.error(request, e.description)
        return redirect("payment_detail", public_id=p.public_id)
    txns = LedgerTransaction.objects.filter(payment=p).prefetch_related("entries__account").order_by("created_at")
    return render(request, "dashboard/payment_detail.html", ctx(
        request, "payments", p=p, events=p.events.all(), refunds=p.refunds.all(), txns=txns))


# ------------------------------------------------------- orders & links
@merchant_required
def orders_list(request):
    qs = Order.objects.filter(merchant=request.merchant).prefetch_related("payments")
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"])
    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    return render(request, "dashboard/orders.html", ctx(request, "orders", orders=page, statuses=Order.STATUS))


@merchant_required
def links(request):
    m = request.merchant
    if request.method == "POST":
        try:
            amount = to_paise(request.POST.get("amount", ""))
            order = services.create_order(
                m, amount, description=request.POST.get("description", "")[:200],
                customer_name=request.POST.get("customer_name", "")[:80],
                customer_email=request.POST.get("customer_email", ""), source="link",
                receipt=request.POST.get("receipt", "")[:60], actor=request.user.username, ip=client_ip(request))
            messages.success(request, f"Payment link created for ₹{format_inr(amount)}.")
        except GatewayError as e:
            messages.error(request, e.description)
        return redirect("links")
    page = Paginator(Order.objects.filter(merchant=m, source="link"), 10).get_page(request.GET.get("page"))
    return render(request, "dashboard/links.html", ctx(request, "links", orders=page,
                                                       base=request.build_absolute_uri("/")[:-1]))


@merchant_required
def refunds_list(request):
    page = Paginator(Refund.objects.filter(merchant=request.merchant).select_related("payment"), 12).get_page(
        request.GET.get("page"))
    total = Refund.objects.filter(merchant=request.merchant).aggregate(s=Sum("amount"))["s"] or 0
    return render(request, "dashboard/refunds.html", ctx(request, "refunds", refunds=page, total=total))


# ------------------------------------------------------------ settlements
@merchant_required
def settlements(request):
    m = request.merchant
    if request.method == "POST":
        stl = ledger.run_settlement(m)
        if stl:
            audit(m, request.user.username, "settlement.created", stl, ip=client_ip(request), net=stl.net)
            messages.success(request, f"Settled ₹{format_inr(stl.net)} to your bank (UTR {stl.utr}).")
        else:
            messages.warning(request, "Nothing to settle right now.")
        return redirect("settlements")
    unsettled = Payment.objects.filter(merchant=m, settlement__isnull=True, status__in=SUCCESS)
    pending_refunds = Refund.objects.filter(merchant=m, settlement__isnull=True).aggregate(s=Sum("amount"))["s"] or 0
    gross = unsettled.aggregate(s=Sum("amount"))["s"] or 0
    fees = unsettled.aggregate(s=Sum("fee"), g=Sum("gst"))
    page = Paginator(Settlement.objects.filter(merchant=m), 10).get_page(request.GET.get("page"))
    return render(request, "dashboard/settlements.html", ctx(
        request, "settlements", settlements=page, unsettled_count=unsettled.count(), gross=gross,
        fees=(fees["s"] or 0) + (fees["g"] or 0), pending_refunds=pending_refunds, payable=ledger.merchant_payable(m)))


@merchant_required
def settlement_detail(request, public_id):
    s = get_object_or_404(Settlement, public_id=public_id, merchant=request.merchant)
    return render(request, "dashboard/settlement_detail.html", ctx(
        request, "settlements", s=s, payments=s.payments.select_related("order"), refunds=s.refunds.select_related("payment")))


# ------------------------------------------------------------------ ledger
@merchant_required
def ledger_view(request):
    m = request.merchant
    rows, td, tc = ledger.trial_balance(m)
    txns = LedgerTransaction.objects.filter(merchant=m).prefetch_related("entries__account")
    page = Paginator(txns, 8).get_page(request.GET.get("page"))
    return render(request, "dashboard/ledger.html", ctx(request, "ledger", rows=rows, td=td, tc=tc,
                                                        balanced=(td == tc), txns=page))


# ---------------------------------------------------------------- webhooks
@merchant_required
def webhooks_view(request):
    m = request.merchant
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_url":
            url = request.POST.get("webhook_url", "").strip()
            if url and not url.startswith(("http://", "https://")):
                messages.error(request, "Webhook URL must start with http:// or https://")
            else:
                m.webhook_url = url
                m.save(update_fields=["webhook_url"])
                audit(m, request.user.username, "webhook.url_changed", m, ip=client_ip(request))
                messages.success(request, "Webhook endpoint saved.")
        elif action == "rotate":
            m.rotate_webhook_secret()
            audit(m, request.user.username, "webhook.secret_rotated", m, ip=client_ip(request))
            messages.success(request, "Signing secret rotated. Update it on your server.")
        elif action == "test":
            if not m.webhook_url:
                messages.error(request, "Save a webhook URL first.")
            else:
                hooks.emit(m, "webhook.test", {"message": "Hello from NovaPay"})
                hooks.process_due()
                messages.success(request, "Test event sent.")
        elif action == "retry":
            d = get_object_or_404(WebhookDelivery, pk=request.POST.get("delivery"), event__merchant=m)
            hooks.retry_now(d)
            messages.success(request, "Delivery re-attempted.")
        elif action == "process":
            n = hooks.process_due()
            messages.success(request, f"Processed {n} due deliveries.")
        return redirect("webhooks")
    deliveries = WebhookDelivery.objects.filter(event__merchant=m).select_related("event").prefetch_related("attempt_log")
    page = Paginator(deliveries, 10).get_page(request.GET.get("page"))
    stats = WebhookDelivery.objects.filter(event__merchant=m).values("status").annotate(c=Count("id"))
    return render(request, "dashboard/webhooks.html", ctx(
        request, "webhooks", deliveries=page, stats={s["status"]: s["c"] for s in stats}))


# --------------------------------------------------------------------- keys
@merchant_required
def keys_view(request):
    m = request.merchant
    if request.method == "POST":
        if request.POST.get("action") == "create":
            key, secret = APIKey.generate(m, request.POST.get("label", "").strip()[:60] or "API key")
            audit(m, request.user.username, "apikey.created", key, ip=client_ip(request))
            request.session["new_key"] = {"key_id": key.key_id, "secret": secret}
            messages.success(request, "New API key created. Copy the secret now — it is shown only once.")
        elif request.POST.get("action") == "revoke":
            key = get_object_or_404(APIKey, pk=request.POST.get("key"), merchant=m)
            key.revoke()
            audit(m, request.user.username, "apikey.revoked", key, ip=client_ip(request))
            messages.success(request, "Key revoked.")
        return redirect("keys")
    new_key = request.session.pop("new_key", None)  # shown once, then gone
    return render(request, "dashboard/keys.html", ctx(request, "keys", keys=m.api_keys.all(), new_key=new_key))


# --------------------------------------------------------------------- risk
@merchant_required
def risk(request):
    m = request.merchant
    if request.method == "POST":
        try:
            m.max_payment_paise = to_paise(request.POST.get("limit", ""))
            m.save(update_fields=["max_payment_paise"])
            audit(m, request.user.username, "risk.limit_changed", m, ip=client_ip(request), limit=m.max_payment_paise)
            messages.success(request, "Per-payment limit updated.")
        except GatewayError as e:
            messages.error(request, e.description)
        return redirect("risk")
    flagged = Payment.objects.filter(merchant=m, fraud_decision__in=["review", "block"]).select_related("order")
    page = Paginator(flagged, 10).get_page(request.GET.get("page"))
    counts = {r["fraud_decision"]: r["c"] for r in Payment.objects.filter(merchant=m).values("fraud_decision").annotate(c=Count("id"))}
    return render(request, "dashboard/risk.html", ctx(request, "risk", flagged=page, counts=counts))


@merchant_required
def audit_view(request):
    page = Paginator(AuditLog.objects.filter(merchant=request.merchant), 15).get_page(request.GET.get("page"))
    return render(request, "dashboard/audit.html", ctx(request, "audit", logs=page))


@merchant_required
def settings_view(request):
    m = request.merchant
    if request.method == "POST":
        m.business_name = request.POST.get("business_name", m.business_name).strip()[:120] or m.business_name
        m.business_type = request.POST.get("business_type", m.business_type)[:60]
        m.website = request.POST.get("website", "")[:200]
        color = request.POST.get("brand_color", m.brand_color)
        if len(color) == 7 and color.startswith("#"):
            m.brand_color = color
        m.save()
        messages.success(request, "Settings saved. Your checkout page now uses your brand colour.")
        return redirect("settings")
    return render(request, "dashboard/settings.html", ctx(request, "settings"))
