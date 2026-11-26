import json
import time
from datetime import timedelta
from unittest import mock
import urllib.error

from django.test import TestCase
from django.utils import timezone

from core.utils import format_inr
from payments import services
from webhooks import services as hooks
from webhooks.models import WebhookDelivery, WebhookEvent
from .base import make_merchant


class FakeResp:
    def __init__(self, status):
        self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


class SignatureTests(TestCase):
    def test_roundtrip_tamper_and_replay(self):
        body = '{"a":1}'
        sig = hooks.sign("whsec_x", body)
        self.assertTrue(hooks.verify_signature("whsec_x", body, sig))
        self.assertFalse(hooks.verify_signature("whsec_x", body + " ", sig))      # tampered body
        self.assertFalse(hooks.verify_signature("whsec_other", body, sig))        # wrong secret
        old = hooks.sign("whsec_x", body, ts=int(time.time()) - 10_000)
        self.assertFalse(hooks.verify_signature("whsec_x", body, old))            # replay outside tolerance
        self.assertFalse(hooks.verify_signature("whsec_x", body, "garbage"))
        self.assertFalse(hooks.verify_signature("whsec_x", body, ""))


class DeliveryTests(TestCase):
    def setUp(self):
        self.m, *_ = make_merchant(webhook_url="http://merchant.test/hook")

    def event(self):
        return hooks.emit(self.m, "payment.captured", {"id": "pay_1"})

    def test_no_url_means_event_but_no_delivery(self):
        self.m.webhook_url = ""
        self.m.save()
        hooks.emit(self.m, "x", {})
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertEqual(WebhookDelivery.objects.count(), 0)

    def test_success_marks_delivered_and_signs_request(self):
        self.event()
        seen = {}
        def fake_open(req, timeout):
            seen["sig"] = req.get_header("X-novapay-signature")
            seen["body"] = req.data.decode()
            return FakeResp(200)
        with mock.patch("urllib.request.urlopen", fake_open):
            self.assertEqual(hooks.process_due(), 1)
        d = WebhookDelivery.objects.get()
        self.assertEqual((d.status, d.attempts), ("delivered", 1))
        self.assertTrue(hooks.verify_signature(self.m.webhook_secret, seen["body"], seen["sig"]))
        self.assertEqual(json.loads(seen["body"])["type"], "payment.captured")
        self.assertEqual(d.attempt_log.count(), 1)

    def test_retry_backoff_then_permanent_failure(self):
        self.event()
        def boom(req, timeout):
            raise urllib.error.URLError("connection refused")
        with mock.patch("urllib.request.urlopen", boom):
            d = WebhookDelivery.objects.get()
            expected = [10, 60, 300, 1800, 7200]
            for i, secs in enumerate(expected, start=1):
                before = timezone.now()
                hooks.attempt_delivery(d)
                self.assertEqual((d.status, d.attempts), ("retrying", i))
                delta = (d.next_attempt_at - before).total_seconds()
                self.assertAlmostEqual(delta, secs, delta=3)
            hooks.attempt_delivery(d)                       # 6th failure -> give up
            self.assertEqual((d.status, d.attempts), ("failed", 6))

    def test_non_2xx_is_failure_and_not_due_until_next_attempt(self):
        self.event()
        err = urllib.error.HTTPError("u", 500, "boom", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            hooks.process_due()
        d = WebhookDelivery.objects.get()
        self.assertEqual((d.status, d.last_response_code), ("retrying", 500))
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("must not be called")):
            self.assertEqual(hooks.process_due(), 0)          # backoff not elapsed
        WebhookDelivery.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        with mock.patch("urllib.request.urlopen", lambda r, timeout: FakeResp(204)):
            self.assertEqual(hooks.process_due(), 1)
        d.refresh_from_db()
        self.assertEqual(d.status, "delivered")

    def test_payment_flow_emits_events(self):
        o = services.create_order(self.m, 50000)
        services.start_payment(o, "upi", {"vpa": "asha@okaxis"}, ip="1.1.1.1")
        types = sorted(WebhookEvent.objects.values_list("event_type", flat=True))
        self.assertEqual(types, ["order.paid", "payment.captured"])
        o2 = services.create_order(self.m, 50000)
        services.start_payment(o2, "upi", {"vpa": "failure@upi"}, ip="1.1.1.1")
        self.assertTrue(WebhookEvent.objects.filter(event_type="payment.failed").exists())

    def test_suppressed_context(self):
        with hooks.suppressed():
            hooks.emit(self.m, "x", {})
        self.assertEqual(WebhookEvent.objects.count(), 0)
        hooks.emit(self.m, "x", {})
        self.assertEqual(WebhookEvent.objects.count(), 1)


class FormatTests(TestCase):
    def test_indian_grouping(self):
        self.assertEqual(format_inr(0), "0.00")
        self.assertEqual(format_inr(99), "0.99")
        self.assertEqual(format_inr(100000), "1,000.00")
        self.assertEqual(format_inr(123456789), "12,34,567.89")
        self.assertEqual(format_inr(-250050), "-2,500.50")
