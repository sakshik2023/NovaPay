# NovaPay — Payment Gateway System (Django)

A complete sandbox payment gateway: hosted checkout, REST API, double-entry ledger, signed webhooks with retries,
fraud rules engine, refunds, settlements, merchant dashboard — plus a demo merchant store that integrates with it
exactly like a real shop would. Runs with **zero external services** (SQLite, no Redis/Celery).

> No real money moves and no real card data is ever stored. Only `last4`, the card network and a salted hash
> (for fraud velocity checks) are kept — never the PAN or CVV.

## Quick start

```bash
python -m venv venv
venv\Scripts\activate            # Windows      (Linux/Mac: source venv/bin/activate)
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo       # demo merchant + 2 weeks of sample data
python manage.py runserver
```

Open http://127.0.0.1:8000

| What | Where |
|---|---|
| Landing page | `/` |
| **Merchant dashboard** | `/login/` → **demo@novapay.test / demo1234** |
| **Demo store** (a merchant website using the API) | `/demo-store/` |
| Hosted checkout | `/pay/<order_id>/` |
| API reference | `/docs/` |

Webhooks are delivered by a background thread that starts automatically with `runserver`.
In production run `python manage.py process_webhooks --loop` as a separate worker.

### Try it (2 minutes)
1. Open **/demo-store/**, add items to the cart, press **Pay with NovaPay**.
2. On the checkout, click a test card in the left panel (autofills). Try `4000 0000 0000 3220` for the OTP (123456) flow.
3. After success you are redirected back; the store **verifies the HMAC signature and confirms the status server-to-server**.
4. Open **/demo-store/orders/** — the order is *Paid* via the **webhook**, shown with its signature check.
5. In the dashboard: Payments → open a payment (timeline, ledger postings, refund), Ledger, Settlements → *Settle now*, Risk.

## Sandbox test data
| Card | Result |
|---|---|
| `4111 1111 1111 1111` | Success (Visa) |
| `5555 5555 5555 4444` | Success (Mastercard) |
| `4000 0000 0000 3220` | 3-D Secure → OTP `123456` |
| `4000 0000 0000 9995` | Declined: insufficient funds |
| `4000 0000 0000 0069` | Declined: card expired |
| `4000 0000 0000 0002` | Declined: generic |
| `4000 0000 0000 0119` | Blocked by fraud engine |

Any future expiry, any 3-digit CVV. UPI: `success@upi`, `failure@upi`, `timeout@upi`.

## Features
- **Hosted checkout** — Luhn/expiry/CVV validation, live card preview, network detection, card + UPI, OTP step, animated results, merchant brand colour, mobile layout
- **REST API v1** — Basic auth with hashed API secrets, orders/payments/refunds/settlements, cursor pagination, uniform error format, per-merchant rate limit (120/min) with `X-RateLimit-*` headers, `X-Request-Id`
- **Idempotency-Key** on every write: retries never double-charge or double-refund
- **Payment state machine** `pending → captured | failed → partially_refunded → refunded` enforced in one place
- **Concurrency-safe money movement** using atomic compare-and-swap updates (parallel refunds / parallel payments tested with real threads)
- **Double-entry ledger** — every posting balances; trial balance; `manage.py check_ledger`
- **Fees & GST** (2% + 18% GST on the fee), **settlements** with UTR, refunds netted, carry-forward of negative balances
- **Webhooks** — HMAC-SHA256 + timestamp (replay protection), persistent queue, backoff 10s→1m→5m→30m→2h, delivery log, manual resend
- **Fraud engine** — blocklist, merchant limit, high value, IP velocity, repeat failures, multi-merchant card use → allow / review / block with reasons
- **Dashboard** — KPI overview, volume chart, method split, searchable/filterable payments, CSV export, payment links, orders, refunds, settlements, ledger, webhooks console, API keys (secret shown once), risk page, audit log, settings
- **Audit log** of sensitive actions (who, what, IP)

## Commands
```bash
python manage.py test tests            # 86 tests (unit, API, race conditions, live HTTP end-to-end)
python manage.py seed_demo --reset     # rebuild demo data
python manage.py check_ledger          # verify debits == credits
python manage.py run_settlements       # settle all merchants (schedule daily)
python manage.py expire_orders         # expire stale unpaid orders
python manage.py process_webhooks --loop
```

## Project layout
```
config/        settings, urls
core/          Merchant, APIKey, Idempotency, AuditLog; auth, throttling, error handling, utils
payments/      Order, Payment, Refund; services (business logic), fraud, bank simulator, REST API
ledger/        accounts, double-entry postings, settlements
webhooks/      events, delivery queue, signing, retry worker
checkout/      hosted checkout (views, JS, CSS)
dashboard/     merchant dashboard views
demostore/     demo merchant site (the "client" of the gateway)
tests/         86 automated tests
docs/          ARCHITECTURE.md (system design + ER diagram), VIVA_QA.md
```
Design docs: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Viva prep: [docs/VIVA_QA.md](docs/VIVA_QA.md)

## Production notes
Switch to PostgreSQL, set `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`, `ALLOWED_HOSTS`, serve behind HTTPS, run the webhook worker as a
service, and use Redis for the rate-limit cache. The service layer (`payments/services.py`) is database-agnostic.
