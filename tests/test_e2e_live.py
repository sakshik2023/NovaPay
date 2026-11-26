"""End-to-end over REAL HTTP: demo store -> NovaPay API -> hosted checkout -> signed webhook back to the store."""
import json
import tempfile
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.test import Client, LiveServerTestCase, override_settings

from demostore import gateway_client
from demostore.models import ReceivedWebhook, StoreOrder
from webhooks import services as hooks
from webhooks.models import WebhookDelivery
from .base import make_merchant


class LiveE2E(LiveServerTestCase):
    def setUp(self):
        self.m, self.key, self.secret = make_merchant("LearnHub", "lh@test.com", webhook_url=self.live_server_url + "/demo-store/webhook/")
        tmp = Path(tempfile.mkdtemp()) / "creds.json"
        tmp.write_text(json.dumps({"key_id": self.key.key_id, "secret": self.secret, "webhook_secret": self.m.webhook_secret}))
        p1 = mock.patch.object(gateway_client, "CRED_FILE", tmp)
        p2 = override_settings(GATEWAY_BASE_URL=self.live_server_url)
        p1.start(); p2.enable()
        self.addCleanup(p1.stop); self.addCleanup(p2.disable)
        self.shop = Client()

    def buy(self):
        self.shop.post("/demo-store/add/py/")
        self.shop.post("/demo-store/add/cv/")
        r = self.shop.post("/demo-store/checkout/")
        self.assertEqual(r.status_code, 302)
        return r["Location"]

    def test_full_purchase_flow(self):
        checkout_url = self.buy()
        self.assertTrue(checkout_url.startswith("/pay/order_"))
        so = StoreOrder.objects.get()
        self.assertEqual((so.status, so.total_paise), ("awaiting_payment", 149900 + 49900))
        self.assertEqual(so.gateway_order_id, checkout_url.strip("/").split("/")[-1])

        # customer pays on the hosted checkout
        pay = self.shop.post(checkout_url + "submit/", json.dumps({"method": "card", "card_number": "4111111111111111", "expiry": "12/30", "cvv": "123"}), content_type="application/json").json()
        self.assertEqual(pay["status"], "captured")

        # webhook reaches the merchant's server over HTTP and flips the order to PAID (browser never returned)
        self.assertEqual(hooks.process_due(), 2)                      # payment.captured + order.paid
        so.refresh_from_db()
        self.assertEqual(so.status, "paid")
        self.assertEqual(ReceivedWebhook.objects.filter(signature_valid=True).count(), 2)
        self.assertEqual(set(WebhookDelivery.objects.values_list("status", flat=True)), {"delivered"})

        # customer redirect: signature verified + status confirmed server-to-server
        r = self.shop.get(pay["return_url"])
        self.assertContains(r, "payment received")
        self.assertContains(r, "Verified (HMAC-SHA256)")

    def test_forged_return_url_is_rejected(self):
        checkout_url = self.buy()
        so = StoreOrder.objects.get()
        forged = f"/demo-store/return/?order_id={so.gateway_order_id}&payment_id=pay_fake&status=captured&signature=deadbeef"
        r = self.shop.get(forged)
        self.assertContains(r, "Could not verify")
        so.refresh_from_db()
        self.assertEqual(so.status, "awaiting_payment")               # a forged redirect never marks it paid

    def test_forged_webhook_is_rejected(self):
        body = json.dumps({"id": "evt_x", "type": "payment.captured", "data": {"id": "pay_x", "order_id": "order_x"}})
        r = self.shop.post("/demo-store/webhook/", body, content_type="application/json", HTTP_X_NOVAPAY_SIGNATURE="t=1,v1=00")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(ReceivedWebhook.objects.get().signature_valid)

    def test_failed_payment_then_retry_succeeds(self):
        checkout_url = self.buy()
        bad = self.shop.post(checkout_url + "submit/", json.dumps({"method": "card", "card_number": "4000000000009995", "expiry": "12/30", "cvv": "123"}), content_type="application/json").json()
        self.assertEqual(bad["status"], "failed")
        r = self.shop.get(bad["return_url"])
        self.assertContains(r, "Payment failed")
        self.assertEqual(StoreOrder.objects.get().status, "payment_failed")
        good = self.shop.post(checkout_url + "submit/", json.dumps({"method": "upi", "vpa": "asha@okaxis"}), content_type="application/json").json()
        self.assertEqual(good["status"], "captured")
        hooks.process_due()
        self.assertEqual(StoreOrder.objects.get().status, "paid")

    def test_webhook_retry_when_merchant_down(self):
        self.m.webhook_url = "http://127.0.0.1:9/never"             # nothing listens on port 9
        self.m.save()
        checkout_url = self.buy()
        self.shop.post(checkout_url + "submit/", json.dumps({"method": "upi", "vpa": "asha@okaxis"}), content_type="application/json")
        hooks.process_due()
        d = WebhookDelivery.objects.first()
        self.assertEqual((d.status, d.attempts), ("retrying", 1))
        self.assertIn("URLError", d.last_error)
        # merchant comes back online -> manual resend succeeds
        self.m.webhook_url = self.live_server_url + "/demo-store/webhook/"
        self.m.save()
        WebhookDelivery.objects.filter(pk=d.pk).update(url=self.m.webhook_url)
        d.refresh_from_db()
        self.assertEqual(hooks.retry_now(d).status, "delivered")
