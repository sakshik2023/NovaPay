import csv
import io
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import APIKey, AuditLog, Merchant
from ledger import services as ledger
from payments import services
from payments.models import Order, Payment
from .base import make_merchant


class UITestBase(TestCase):
    def setUp(self):
        self.m, self.key, self.secret = make_merchant()
        self.web = Client()
        self.web.login(username="acme@test.com", password="pass12345")
        self.paid = self.make_payment(150000, "card")
        self.failed = self.make_payment(50000, "card", card="4000000000009995")
        self.upi = self.make_payment(75000, "upi")

    def make_payment(self, amount, method, card="4111111111111111"):
        o = services.create_order(self.m, amount, receipt=f"R{amount}", description="Test item")
        data = {"card_number": card, "expiry": "12/30", "cvv": "123"} if method == "card" else {"vpa": "asha@okaxis"}
        return services.start_payment(o, method, data, ip=f"5.5.5.{amount % 200}")


class DashboardTests(UITestBase):
    def test_every_page_renders(self):
        stl = None
        urls = ["overview", "payments", "orders", "links", "refunds", "settlements", "ledger", "webhooks", "keys", "risk", "audit", "settings"]
        for name in urls:
            r = self.web.get(reverse(name))
            self.assertEqual(r.status_code, 200, name)
        self.assertEqual(self.web.get(reverse("payment_detail", args=[self.paid.public_id])).status_code, 200)

    def test_login_required(self):
        anon = Client()
        for name in ["overview", "payments", "keys", "ledger"]:
            r = anon.get(reverse(name))
            self.assertEqual(r.status_code, 302)
            self.assertIn("/login/", r["Location"])

    def test_overview_numbers(self):
        r = self.web.get(reverse("overview"))
        self.assertEqual(r.context["volume"], 225000)                # 150000 + 75000 successful
        self.assertEqual(r.context["n_ok"], 2)
        self.assertEqual(r.context["n_fail"], 1)
        self.assertEqual(r.context["success_rate"], 67)
        self.assertContains(r, "₹2,250.00")
        self.assertEqual(len(r.context["bars"]), 14)

    def test_payment_filters_and_search(self):
        def ids(**q):
            return {p.public_id for p in self.web.get(reverse("payments"), q).context["payments"]}
        self.assertEqual(ids(status="failed"), {self.failed.public_id})
        self.assertEqual(ids(method="upi"), {self.upi.public_id})
        self.assertEqual(ids(q=self.paid.public_id), {self.paid.public_id})
        self.assertEqual(ids(q="1111"), {self.paid.public_id})
        self.assertEqual(len(ids()), 3)
        tomorrow = (timezone.now() + timedelta(days=2)).date().isoformat()
        self.assertEqual(ids(**{"from": tomorrow}), set())

    def test_csv_export(self):
        r = self.web.get(reverse("payments"), {"export": "csv", "status": "captured"})
        self.assertEqual(r["Content-Type"], "text/csv")
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        self.assertEqual(rows[0][0], "payment_id")
        self.assertEqual(len(rows), 3)                               # header + 2 captured
        self.assertNotIn("4111111111111111", r.content.decode())

    def test_refund_from_dashboard(self):
        url = reverse("payment_detail", args=[self.paid.public_id])
        r = self.web.post(url, {"amount": "500.00", "reason": "Late delivery"}, follow=True)
        self.assertContains(r, "Refund processed")
        self.paid.refresh_from_db()
        self.assertEqual((self.paid.status, self.paid.refunded_amount), ("partially_refunded", 50000))
        r = self.web.post(url, {"amount": "99999"}, follow=True)
        self.assertContains(r, "exceeds refundable")
        r = self.web.post(url, {"amount": "abc"}, follow=True)
        self.assertContains(r, "valid amount")
        self.web.post(url, {})                                        # empty -> refund the rest
        self.paid.refresh_from_db()
        self.assertEqual(self.paid.status, "refunded")
        self.assertTrue(AuditLog.objects.filter(action="refund.created").exists())

    def test_other_merchants_payment_is_404(self):
        m2, *_ = make_merchant("Other", "other@test.com")
        o = services.create_order(m2, 5000)
        p = services.start_payment(o, "upi", {"vpa": "asha@okaxis"}, ip="8.8.8.8")
        self.assertEqual(self.web.get(reverse("payment_detail", args=[p.public_id])).status_code, 404)

    def test_settlement_button(self):
        r = self.web.post(reverse("settlements"), follow=True)
        self.assertContains(r, "Settled")
        self.assertEqual(ledger.merchant_payable(self.m), 0)
        r = self.web.post(reverse("settlements"), follow=True)
        self.assertContains(r, "Nothing to settle")
        stl = self.m.settlements.get()
        self.assertEqual(self.web.get(reverse("settlement_detail", args=[stl.public_id])).status_code, 200)

    def test_ledger_page_balanced(self):
        r = self.web.get(reverse("ledger"))
        self.assertTrue(r.context["balanced"])
        self.assertContains(r, "Books balanced")

    def test_api_key_secret_shown_once(self):
        r = self.web.post(reverse("keys"), {"action": "create", "label": "CI"}, follow=True)
        secret = r.context["new_key"]["secret"]
        self.assertTrue(secret.startswith("sk_"))
        self.assertContains(r, secret)
        again = self.web.get(reverse("keys"))
        self.assertNotContains(again, secret)
        k = APIKey.objects.get(label="CI")
        self.web.post(reverse("keys"), {"action": "revoke", "key": k.pk})
        k.refresh_from_db()
        self.assertFalse(k.is_active)

    def test_cannot_revoke_other_merchants_key(self):
        _, k2, _ = make_merchant("Other", "other@test.com")
        r = self.web.post(reverse("keys"), {"action": "revoke", "key": k2.pk})
        self.assertEqual(r.status_code, 404)

    def test_webhook_settings(self):
        self.web.post(reverse("webhooks"), {"action": "save_url", "webhook_url": "https://x.test/h"})
        self.m.refresh_from_db()
        self.assertEqual(self.m.webhook_url, "https://x.test/h")
        old = self.m.webhook_secret
        self.web.post(reverse("webhooks"), {"action": "rotate"})
        self.m.refresh_from_db()
        self.assertNotEqual(self.m.webhook_secret, old)
        r = self.web.post(reverse("webhooks"), {"action": "save_url", "webhook_url": "javascript:alert(1)"}, follow=True)
        self.assertContains(r, "must start with http")

    def test_risk_limit_and_payment_link(self):
        self.web.post(reverse("risk"), {"limit": "1000"})
        self.m.refresh_from_db()
        self.assertEqual(self.m.max_payment_paise, 100000)
        self.web.post(reverse("links"), {"amount": "250.50", "description": "Fee"})
        o = Order.objects.filter(source="link").get()
        self.assertEqual(o.amount, 25050)
        r = self.web.post(reverse("links"), {"amount": "-3"}, follow=True)
        self.assertContains(r, "positive")


class AuthFlowTests(TestCase):
    def test_register_creates_merchant_and_key(self):
        c = Client()
        r = c.post("/register/", {"business_name": "Chai Co", "email": "chai@x.com", "password": "longenough1", "business_type": "Retail"}, follow=True)
        self.assertEqual(r.status_code, 200)
        m = Merchant.objects.get(business_name="Chai Co")
        self.assertEqual(m.api_keys.count(), 1)
        self.assertContains(r, "sk_")                                 # secret shown once on the keys page

    def test_register_validation(self):
        make_merchant(email="dup@x.com")
        c = Client()
        r = c.post("/register/", {"business_name": "A", "email": "dup@x.com", "password": "short"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.context["errors"]), {"business_name", "email", "password"})

    def test_login_and_logout(self):
        make_merchant()
        c = Client()
        self.assertEqual(c.post("/login/", {"username": "acme@test.com", "password": "nope"}).status_code, 200)
        r = c.post("/login/", {"username": "acme@test.com", "password": "pass12345"})
        self.assertRedirects(r, "/dashboard/", fetch_redirect_response=False)
        c.post("/logout/")
        self.assertEqual(c.get("/dashboard/").status_code, 302)

    def test_public_pages(self):
        c = Client()
        for url in ["/", "/docs/", "/login/", "/register/", "/demo-store/"]:
            self.assertEqual(c.get(url).status_code, 200, url)


class CheckoutTests(UITestBase):
    def order(self, **kw):
        return services.create_order(self.m, 99900, description="Course", callback_url="https://shop.test/return", **kw)

    def submit(self, order, body):
        return self.web.post(f"/pay/{order.public_id}/submit/", json.dumps(body), content_type="application/json")

    def test_page_renders_with_amount_and_branding(self):
        o = self.order()
        r = self.web.get(f"/pay/{o.public_id}/")
        self.assertContains(r, "₹999.00")
        self.assertContains(r, self.m.brand_color)
        self.assertEqual(r.context["state"], "pay")

    def test_unknown_order_404(self):
        self.assertEqual(self.web.get("/pay/order_nope/").status_code, 404)

    def test_success_returns_signed_redirect(self):
        from urllib.parse import parse_qs, urlparse
        o = self.order()
        r = self.submit(o, {"method": "card", "card_number": "4111 1111 1111 1111", "expiry": "12/30", "cvv": "123"})
        j = r.json()
        self.assertEqual(j["status"], "captured")
        q = parse_qs(urlparse(j["return_url"]).query)
        p = Payment.objects.get(public_id=j["payment_id"])
        self.assertEqual(q["signature"][0], services.callback_signature(o, p))
        self.assertEqual(self.web.get(f"/pay/{o.public_id}/").context["state"], "paid")

    def test_failure_payload_and_validation_errors(self):
        o = self.order()
        j = self.submit(o, {"method": "card", "card_number": "4000000000009995", "expiry": "12/30", "cvv": "123"}).json()
        self.assertEqual((j["status"], j["error_code"]), ("failed", "INSUFFICIENT_FUNDS"))
        r = self.submit(o, {"method": "card", "card_number": "1234", "expiry": "12/30", "cvv": "123"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["field"], "card_number")
        self.assertEqual(self.submit(o, {"method": "upi", "vpa": "success@upi"}).json()["status"], "captured")

    def test_otp_endpoint(self):
        o = self.order()
        j = self.submit(o, {"method": "card", "card_number": "4000000000003220", "expiry": "12/30", "cvv": "123"}).json()
        self.assertTrue(j["needs_otp"])
        bad = self.web.post(f"/pay/{o.public_id}/otp/", json.dumps({"payment_id": j["payment_id"], "otp": "1"}), content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        ok = self.web.post(f"/pay/{o.public_id}/otp/", json.dumps({"payment_id": j["payment_id"], "otp": "123456"}), content_type="application/json")
        self.assertEqual(ok.json()["status"], "captured")

    def test_otp_for_payment_of_other_order_rejected(self):
        o1, o2 = self.order(), self.order()
        j = self.submit(o1, {"method": "card", "card_number": "4000000000003220", "expiry": "12/30", "cvv": "123"}).json()
        r = self.web.post(f"/pay/{o2.public_id}/otp/", json.dumps({"payment_id": j["payment_id"], "otp": "123456"}), content_type="application/json")
        self.assertEqual(r.status_code, 404)

    def test_expired_link(self):
        o = self.order()
        Order.objects.filter(pk=o.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.web.get(f"/pay/{o.public_id}/").context["state"], "expired")
        r = self.submit(o, {"method": "upi", "vpa": "asha@okaxis"})
        self.assertEqual(r.status_code, 410)

    def test_bad_json(self):
        o = self.order()
        r = self.web.post(f"/pay/{o.public_id}/submit/", "{not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_csrf_enforced_on_checkout(self):
        o = self.order()
        strict = Client(enforce_csrf_checks=True)
        r = strict.post(f"/pay/{o.public_id}/submit/", json.dumps({"method": "upi", "vpa": "asha@okaxis"}), content_type="application/json")
        self.assertEqual(r.status_code, 403)
