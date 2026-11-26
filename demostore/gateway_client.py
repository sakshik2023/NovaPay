"""How a real merchant integrates: server-side calls to the NovaPay REST API with HTTP Basic auth."""
import base64
import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from django.conf import settings

CRED_FILE = Path(settings.BASE_DIR) / ".demo_credentials.json"


def credentials():
    try:
        return json.loads(CRED_FILE.read_text())
    except (OSError, ValueError):
        return None


def call(method, path, body=None, idempotency_key=None):
    creds = credentials()
    if not creds:
        raise RuntimeError("Demo credentials missing. Run: python manage.py seed_demo")
    token = base64.b64encode(f"{creds['key_id']}:{creds['secret']}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(settings.GATEWAY_BASE_URL + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def create_order(amount, receipt, description, callback_url):
    return call("POST", "/v1/orders",
                {"amount": amount, "receipt": receipt, "description": description, "callback_url": callback_url},
                idempotency_key="store-" + receipt)


def fetch_payment(payment_id):
    return call("GET", f"/v1/payments/{payment_id}")
