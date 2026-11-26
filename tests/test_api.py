from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import IdempotencyRecord
from ledger import services as ledger
from ledger.models import LedgerEntry
from payments.models import Order, Payment, Refund
from .base import OK_CARD, api_client, make_merchant


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.m, self.key, self.secret = make_merchant()
        self.c = api_client(self.key, self.secret)

    def order(self, amount=100000, **kw):
        r = self.c.post("/v1/orders", {"amount": amount, **kw}, format="json")
        assert r.status_code == 201, r.content
        return r.json()

    def pay(self, order_id, **data):
        body = {**OK_CARD, "order_id": order_id, **data}
        return self.c.post("/v1/payments", body, format="json")


class AuthTests(Base):
    def test_no_credentials(self):
        r = APIClient().get("/v1/ping")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], "AUTHENTICATION_ERROR")

    def test_wrong_secret(self):
        bad = api_client(self.key, "sk_wrong")
        self.assertEqual(bad.get("/v1/ping").status_code, 401)

    def test_valid_and_last_used_updated(self):
        self.assertEqual(self.c.get("/v1/ping").status_code, 200)
        self.key.refresh_from_db()
        self.assertIsNotNone(self.key.last_used_at)

    def test_revoked_key_rejected(self):
        self.key.revoke()
        self.assertEqual(self.c.get("/v1/ping").status_code, 401)

    def test_secret_not_stored_in_plaintext(self):
        self.assertNotEqual(self.key.secret_hash, self.secret)
        self.assertEqual(len(self.key.secret_hash), 64)


class OrderTests(Base):
    def test_create_and_fetch(self):
        o = self.order(250000, receipt="INV-1")
        self.assertEqual(o["status"], "created")
        self.assertEqual(o["checkout_url"], f"/pay/{o['id']}/")
        r = self.c.get(f"/v1/orders/{o['id']}")
        self.assertEqual(r.json()["receipt"], "INV-1")

    def test_amount_validation(self):
        for bad in [0, 99, -5, 12.5, "500", None, 999_999_999_999]:
            r = self.c.post("/v1/orders", {"amount": bad}, format="json")
            self.assertEqual(r.status_code, 400, bad)
            self.assertEqual(r.json()["error"]["field"], "amount")

    def test_missing_amount_and_bad_callback(self):
        self.assertEqual(self.c.post("/v1/orders", {}, format="json").status_code, 400)
        r = self.c.post("/v1/orders", {"amount": 500, "callback_url": "ftp://x"}, format="json")
        self.assertEqual(r.json()["error"]["field"], "callback_url")

    def test_list_pagination(self):
        for _ in range(5):
            self.order(500)
        r = self.c.get("/v1/orders?count=2").json()
        self.assertEqual(r["count"], 2)
        self.assertTrue(r["has_more"])
        nxt = self.c.get(f"/v1/orders?count=10&starting_after={r['items'][-1]['id']}").json()
        self.assertEqual(nxt["count"], 3)
        self.assertFalse(nxt["has_more"])

    def test_tenant_isolation(self):
        o = self.order()
        _, k2, s2 = make_merchant("Other", "other@test.com")
        other = api_client(k2, s2)
        self.assertEqual(other.get(f"/v1/orders/{o['id']}").status_code, 404)
        self.assertEqual(other.get("/v1/orders").json()["count"], 0)


class IdempotencyTests(Base):
    def test_replay_returns_same_order_once(self):
        h = {"HTTP_IDEMPOTENCY_KEY": "k-1"}
        a = self.c.post("/v1/orders", {"amount": 5000}, format="json", **h)
        b = self.c.post("/v1/orders", {"amount": 5000}, format="json", **h)
        self.assertEqual(a.json()["id"], b.json()["id"])
        self.assertEqual(b["Idempotent-Replayed"], "true")
        self.assertEqual(Order.objects.count(), 1)

    def test_same_key_different_body_rejected(self):
        h = {"HTTP_IDEMPOTENCY_KEY": "k-2"}
        self.c.post("/v1/orders", {"amount": 5000}, format="json", **h)
        r = self.c.post("/v1/orders", {"amount": 9999}, format="json", **h)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "IDEMPOTENCY_KEY_REUSED")

    def test_failed_request_does_not_burn_key(self):
        h = {"HTTP_IDEMPOTENCY_KEY": "k-3"}
        self.assertEqual(self.c.post("/v1/orders", {"amount": 1}, format="json", **h).status_code, 400)
        self.assertEqual(self.c.post("/v1/orders", {"amount": 1}, format="json", **h).status_code, 400)
        self.assertFalse(IdempotencyRecord.objects.filter(key="k-3").exists())

    def test_payment_not_charged_twice(self):
        o = self.order()
        h = {"HTTP_IDEMPOTENCY_KEY": "pay-1"}
        a = self.pay(o["id"], **{}) if False else self.c.post("/v1/payments", {**OK_CARD, "order_id": o["id"]}, format="json", **h)
        b = self.c.post("/v1/payments", {**OK_CARD, "order_id": o["id"]}, format="json", **h)
        self.assertEqual(a.json()["id"], b.json()["id"])
        self.assertEqual(Payment.objects.count(), 1)

    def test_scoped_per_merchant(self):
        _, k2, s2 = make_merchant("Other", "other@test.com")
        other = api_client(k2, s2)
        h = {"HTTP_IDEMPOTENCY_KEY": "same"}
        a = self.c.post("/v1/orders", {"amount": 5000}, format="json", **h)
        b = other.post("/v1/orders", {"amount": 5000}, format="json", **h)
        self.assertNotEqual(a.json()["id"], b.json()["id"])


class PaymentTests(Base):
    def test_card_success_fee_math_and_ledger(self):
        o = self.order(100000)
        r = self.pay(o["id"])
        self.assertEqual(r.status_code, 201)
        p = r.json()
        self.assertEqual(p["status"], "captured")
        self.assertEqual((p["fee"], p["gst"], p["net_amount"]), (2000, 360, 97640))
        self.assertEqual(p["card"], {"network": "visa", "last4": "1111"})
        self.assertNotIn("4111111111111111", r.content.decode())
        self.assertEqual(self.c.get(f"/v1/orders/{o['id']}").json()["status"], "paid")
        pay = Payment.objects.get()
        entries = {(e.account.code, e.direction): e.amount for e in LedgerEntry.objects.select_related("account")}
        self.assertEqual(entries[("BANK", "D")], 100000)
        self.assertEqual(entries[("MERCHANT_PAYABLE", "C")], 97640)
        self.assertEqual(entries[("FEE_REVENUE", "C")], 2000)
        self.assertEqual(entries[("GST_PAYABLE", "C")], 360)
        self.assertEqual(ledger.verify_ledger(), [])
        self.assertEqual(ledger.merchant_payable(self.m), 97640)
        self.assertFalse(hasattr(pay, "card_number"))

    def test_declines(self):
        cases = {"4000000000009995": "INSUFFICIENT_FUNDS", "4000000000000069": "CARD_EXPIRED", "4000000000000002": "CARD_DECLINED"}
        for card, code in cases.items():
            o = self.order()
            p = self.pay(o["id"], card_number=card).json()
            self.assertEqual((p["status"], p["error_code"]), ("failed", code))
        self.assertEqual(LedgerEntry.objects.count(), 0)  # failures never touch the ledger

    def test_failed_payment_can_be_retried_on_same_order(self):
        o = self.order()
        self.assertEqual(self.pay(o["id"], card_number="4000000000009995").json()["status"], "failed")
        self.assertEqual(self.pay(o["id"]).json()["status"], "captured")
        self.assertEqual(Payment.objects.filter(order__public_id=o["id"]).count(), 2)

    def test_card_validation(self):
        o = self.order()
        for patch, field in [({"card_number": "4111111111111112"}, "card_number"), ({"card_number": "12"}, "card_number"),
                             ({"expiry": "01/20"}, "expiry"), ({"expiry": "13/30"}, "expiry"), ({"expiry": "abc"}, "expiry"),
                             ({"cvv": "12"}, "cvv"), ({"cvv": "abcd"}, "cvv")]:
            r = self.pay(o["id"], **patch)
            self.assertEqual(r.status_code, 400, patch)
            self.assertEqual(r.json()["error"]["field"], field)
        self.assertEqual(Payment.objects.count(), 0)

    def test_upi_paths(self):
        for vpa, status in [("asha@okaxis", "captured"), ("failure@upi", "failed"), ("timeout@upi", "failed")]:
            o = self.order()
            p = self.c.post("/v1/payments", {"order_id": o["id"], "method": "upi", "vpa": vpa}, format="json").json()
            self.assertEqual(p["status"], status, vpa)
        o = self.order()
        r = self.c.post("/v1/payments", {"order_id": o["id"], "method": "upi", "vpa": "not a vpa"}, format="json")
        self.assertEqual(r.json()["error"]["field"], "vpa")

    def test_unsupported_method(self):
        o = self.order()
        r = self.c.post("/v1/payments", {"order_id": o["id"], "method": "crypto"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_otp_flow(self):
        o = self.order()
        r = self.pay(o["id"], card_number="4000000000003220").json()
        self.assertEqual(r["status"], "pending")
        self.assertEqual(r["next_action"]["type"], "otp")
        bad = self.c.post(f"/v1/payments/{r['id']}/otp", {"otp": "000000"}, format="json")
        self.assertEqual(bad.json()["error"]["code"], "INVALID_OTP")
        ok = self.c.post(f"/v1/payments/{r['id']}/otp", {"otp": "123456"}, format="json").json()
        self.assertEqual(ok["status"], "captured")
        again = self.c.post(f"/v1/payments/{r['id']}/otp", {"otp": "123456"}, format="json")
        self.assertEqual(again.status_code, 409)

    def test_otp_three_strikes_fails_payment(self):
        o = self.order()
        r = self.pay(o["id"], card_number="4000000000003220").json()
        for _ in range(3):
            self.c.post(f"/v1/payments/{r['id']}/otp", {"otp": "111111"}, format="json")
        self.assertEqual(self.c.get(f"/v1/payments/{r['id']}").json()["status"], "failed")
        self.assertEqual(self.c.get(f"/v1/payments/{r['id']}").json()["error_code"], "OTP_ATTEMPTS_EXCEEDED")

    def test_paid_order_cannot_be_paid_again(self):
        o = self.order()
        self.pay(o["id"])
        r = self.pay(o["id"])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "ORDER_ALREADY_PAID")

    def test_expired_order(self):
        from datetime import timedelta
        from django.utils import timezone
        o = self.order()
        Order.objects.filter(public_id=o["id"]).update(expires_at=timezone.now() - timedelta(minutes=1))
        r = self.pay(o["id"])
        self.assertEqual(r.status_code, 410)
        self.assertEqual(r.json()["error"]["code"], "ORDER_EXPIRED")

    def test_payment_tenant_isolation_and_filters(self):
        o = self.order()
        p = self.pay(o["id"]).json()
        _, k2, s2 = make_merchant("Other", "other@test.com")
        self.assertEqual(api_client(k2, s2).get(f"/v1/payments/{p['id']}").status_code, 404)
        self.assertEqual(self.c.get("/v1/payments?status=captured").json()["count"], 1)
        self.assertEqual(self.c.get("/v1/payments?status=failed").json()["count"], 0)
        self.assertEqual(self.c.get("/v1/payments?method=upi").json()["count"], 0)

    def test_state_machine(self):
        from core.exceptions import InvalidTransition
        o = self.order()
        pay = Payment.objects.get(public_id=self.pay(o["id"]).json()["id"])
        with self.assertRaises(InvalidTransition):
            pay.transition("pending")
        with self.assertRaises(InvalidTransition):
            pay.transition("failed")
        pay.transition("partially_refunded")


class RefundTests(Base):
    def captured(self, amount=100000):
        o = self.order(amount)
        return self.pay(o["id"]).json()

    def test_partial_then_full(self):
        p = self.captured()
        r1 = self.c.post(f"/v1/payments/{p['id']}/refunds", {"amount": 30000}, format="json")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(self.c.get(f"/v1/payments/{p['id']}").json()["status"], "partially_refunded")
        r2 = self.c.post(f"/v1/payments/{p['id']}/refunds", {}, format="json")   # remaining 70000
        self.assertEqual(r2.json()["amount"], 70000)
        d = self.c.get(f"/v1/payments/{p['id']}").json()
        self.assertEqual((d["status"], d["amount_refunded"]), ("refunded", 100000))
        self.assertEqual(ledger.verify_ledger(), [])

    def test_over_refund_rejected(self):
        p = self.captured()
        self.c.post(f"/v1/payments/{p['id']}/refunds", {"amount": 60000}, format="json")
        r = self.c.post(f"/v1/payments/{p['id']}/refunds", {"amount": 50000}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "REFUND_EXCEEDS_AMOUNT")
        self.assertEqual(Refund.objects.count(), 1)

    def test_invalid_amounts(self):
        p = self.captured()
        for bad in [0, -1, 1.5, "100"]:
            r = self.c.post(f"/v1/payments/{p['id']}/refunds", {"amount": bad}, format="json")
            self.assertEqual(r.status_code, 400, bad)

    def test_cannot_refund_failed_or_fully_refunded(self):
        o = self.order()
        failed = self.pay(o["id"], card_number="4000000000009995").json()
        self.assertEqual(self.c.post(f"/v1/payments/{failed['id']}/refunds", {}, format="json").status_code, 409)
        p = self.captured()
        self.c.post(f"/v1/payments/{p['id']}/refunds", {}, format="json")
        self.assertEqual(self.c.post(f"/v1/payments/{p['id']}/refunds", {}, format="json").status_code, 409)

    def test_refund_idempotent(self):
        p = self.captured()
        h = {"HTTP_IDEMPOTENCY_KEY": "rf-1"}
        a = self.c.post(f"/v1/payments/{p['id']}/refunds", {"amount": 1000}, format="json", **h)
        b = self.c.post(f"/v1/payments/{p['id']}/refunds", {"amount": 1000}, format="json", **h)
        self.assertEqual(a.json()["id"], b.json()["id"])
        self.assertEqual(Refund.objects.count(), 1)

    def test_other_merchant_cannot_refund(self):
        p = self.captured()
        _, k2, s2 = make_merchant("Other", "other@test.com")
        self.assertEqual(api_client(k2, s2).post(f"/v1/payments/{p['id']}/refunds", {}, format="json").status_code, 404)


class FraudTests(Base):
    def test_blocklisted_card(self):
        o = self.order()
        p = self.pay(o["id"], card_number="4000000000000119").json()
        self.assertEqual((p["status"], p["error_code"]), ("failed", "FRAUD_BLOCKED"))
        self.assertEqual(p["fraud"]["decision"], "block")
        self.assertEqual(LedgerEntry.objects.count(), 0)

    def test_merchant_limit(self):
        self.m.max_payment_paise = 50000
        self.m.save()
        o = self.order(60000)
        self.assertEqual(self.pay(o["id"]).json()["error_code"], "FRAUD_BLOCKED")

    def test_high_value_flagged_but_allowed_below_review(self):
        o = self.order(5_000_000)
        p = self.pay(o["id"]).json()
        self.assertEqual(p["status"], "captured")
        self.assertEqual(p["fraud"]["score"], 20)

    def test_ip_velocity_marks_review(self):
        for _ in range(5):
            o = self.order(1000)
            self.pay(o["id"])
        # 5 earlier attempts from the same (test client) IP -> +45 => review but still processed
        o = self.order(1000)
        p = self.pay(o["id"]).json()
        self.assertEqual(p["fraud"]["decision"], "review")
        self.assertEqual(p["status"], "captured")

    def test_repeated_failures_raise_score(self):
        for _ in range(3):
            o = self.order(1000)
            self.pay(o["id"], card_number="4000000000009995")
        o = self.order(1000)
        p = self.pay(o["id"], card_number="4000000000009995").json()
        self.assertGreaterEqual(p["fraud"]["score"], 50)


class SettlementTests(Base):
    def test_settlement_matches_ledger_and_is_idempotent(self):
        pays = [self.pay(self.order(a)["id"]).json() for a in (100000, 250000, 50000)]
        self.c.post(f"/v1/payments/{pays[0]['id']}/refunds", {"amount": 20000}, format="json")
        payable = ledger.merchant_payable(self.m)
        stl = ledger.run_settlement(self.m)
        self.assertEqual(stl.net, payable)
        self.assertEqual(stl.gross, 400000)
        self.assertEqual(stl.refunds_total, 20000)
        self.assertEqual(stl.net, stl.gross - stl.fees - stl.refunds_total)
        self.assertEqual(ledger.merchant_payable(self.m), 0)
        self.assertIsNone(ledger.run_settlement(self.m))
        self.assertEqual(Payment.objects.filter(settlement=stl).count(), 3)
        self.assertEqual(ledger.verify_ledger(), [])
        _, d, c = ledger.trial_balance()
        self.assertEqual(d, c)

    def test_late_refund_goes_to_next_settlement(self):
        p = self.pay(self.order(100000)["id"]).json()
        first = ledger.run_settlement(self.m)
        self.assertEqual(first.net, 97640)
        self.c.post(f"/v1/payments/{p['id']}/refunds", {"amount": 10000}, format="json")
        self.assertEqual(ledger.merchant_payable(self.m), -10000)
        # nothing positive to pay out -> no settlement, debt carried forward
        self.assertIsNone(ledger.run_settlement(self.m))
        self.pay(self.order(100000)["id"])
        second = ledger.run_settlement(self.m)
        self.assertEqual(second.refunds_total, 10000)
        self.assertEqual(second.net, 97640 - 10000)
        self.assertEqual(ledger.merchant_payable(self.m), 0)

    def test_settlements_api(self):
        self.pay(self.order()["id"])
        ledger.run_settlement(self.m)
        r = self.c.get("/v1/settlements").json()
        self.assertEqual(r["count"], 1)
        self.assertTrue(r["items"][0]["utr"].startswith("UTR"))


class ThrottleTests(Base):
    def test_rate_limit_and_headers(self):
        rest = {"DEFAULT_THROTTLE_RATES": {"merchant": "3/min"}}
        from django.conf import settings
        with override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, **rest}):
            codes, last = [], None
            for _ in range(5):
                last = self.c.get("/v1/ping")
                codes.append(last.status_code)
            self.assertEqual(codes, [200, 200, 200, 429, 429])
            self.assertEqual(last.json()["error"]["code"], "RATE_LIMITED")
            self.assertIn("Retry-After", last)

    def test_headers_present(self):
        r = self.c.get("/v1/ping")
        self.assertIn("X-RateLimit-Remaining", r)
        self.assertIn("X-Request-Id", r)
