import base64

from django.contrib.auth.models import User
from rest_framework.test import APIClient

from core.models import APIKey, Merchant


def make_merchant(name="Acme Store", email="acme@test.com", webhook_url="", max_payment=10_000_000):
    user = User.objects.create_user(username=email, email=email, password="pass12345")
    m = Merchant.objects.create(user=user, business_name=name, webhook_url=webhook_url, max_payment_paise=max_payment)
    key, secret = APIKey.generate(m)
    return m, key, secret


def api_client(key, secret):
    c = APIClient()
    token = base64.b64encode(f"{key.key_id}:{secret}".encode()).decode()
    c.credentials(HTTP_AUTHORIZATION=f"Basic {token}")
    return c


OK_CARD = {"method": "card", "card_number": "4111111111111111", "expiry": "12/30", "cvv": "123"}
