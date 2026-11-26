# Viva preparation — NovaPay

## 30-second pitch
"NovaPay is a payment gateway like Razorpay, built in Django. Merchants integrate via a REST API or a hosted checkout. It handles
card/UPI payments through a bank simulator, runs fraud checks, keeps a double-entry ledger, sends signed webhooks, supports refunds
and settlements, and has a merchant dashboard. The hard parts I solved were idempotency, race conditions in money movement, and
keeping the books balanced."

## Demo script (5 minutes)
1. `/demo-store/` → add to cart → Pay with NovaPay (shows a real merchant integration)
2. Checkout: type a card (live preview) → use `4000 0000 0000 3220` → OTP `123456` → success + redirect
3. `/demo-store/orders/` → order Paid via **webhook** with signature ✓
4. Dashboard → payment detail: timeline, risk score, **ledger postings (debit = credit)** → issue a partial refund
5. Ledger → "Books balanced" · Settlements → Settle now (fees + GST + refunds netted, UTR)
6. Try `4000 0000 0000 0119` → blocked by fraud · Webhooks page → delivery log and resend
7. Terminal: `python manage.py test tests` → 86 passed

## Likely questions

**Why store money as integer paise?** Floats cannot represent decimals exactly (0.1 + 0.2 ≠ 0.3); integers make fee/GST maths exact and ledger sums balance to the paisa.

**What is idempotency and where is it?** A retried request must not repeat its side effect. The client sends `Idempotency-Key`; I store (merchant, key, request hash, response). Same key + same body → replay stored response; same key + different body → 422. See `core/idempotency.py`.

**How do you prevent a customer being charged twice / refunded more than paid?** Critical changes are single atomic conditional `UPDATE`s (compare-and-swap), e.g. `UPDATE payment SET refunded = refunded + x WHERE refunded <= amount − x`. If 0 rows change, the request lost the race. Tested with 8 parallel threads; removing the guard makes the test fail.

**Explain double-entry bookkeeping.** Every transaction has equal debits and credits across accounts, so money is never created or lost. A ₹1000 capture debits Bank 1000 and credits Merchant Payable 976.40, Fee Revenue 20, GST Payable 3.60. `check_ledger` verifies the invariant.

**Why a state machine for payments?** It makes illegal transitions impossible (e.g. refunding a failed payment, capturing twice). Allowed moves are defined once in `Payment.ALLOWED`.

**How are webhooks secured and made reliable?** HMAC-SHA256 over `timestamp.body` with a per-merchant secret; the receiver recomputes it and rejects stale timestamps (replay protection). Delivery is a persisted queue with exponential backoff and a delivery log; unreachable merchants don't lose events.

**Why verify the redirect AND call the API?** The redirect URL can be forged by the customer. The merchant verifies the HMAC signature, then asks the gateway server-to-server for the real status. (Demonstrated in `demostore/views.py::payment_return`; forged URL is rejected in tests.)

**How do you protect card data?** The PAN is validated and sent to the simulator in memory only; I store last4, network, and a salted hash for velocity checks. CVV is never stored. Real gateways must be PCI-DSS compliant — this is a sandbox.

**How does the fraud engine work?** Independent rules add risk points (blocklist, limits, IP velocity, repeated failures, same card across merchants). ≥80 block, 40–79 review (processed but flagged), else allow. Reasons are stored and shown to the merchant.

**How are API keys stored?** Only a SHA-256 hash of the secret; shown once at creation; verified with constant-time comparison (`hmac.compare_digest`) to avoid timing attacks.

**How is multi-tenancy enforced?** Every query is scoped by the authenticated merchant; accessing another merchant's object returns 404 (tested).

**Why SQLite here, and what changes in production?** SQLite keeps the project zero-setup. I use `BEGIN IMMEDIATE` so concurrent writers queue. For production switch to PostgreSQL (row-level locks), Redis for rate limiting and a real worker for webhooks — the service layer doesn't change.

**Why not Celery?** To keep the project runnable anywhere. The webhook queue is a DB table with a worker loop (thread or `process_webhooks --loop`) — same architecture, swappable for Celery.

**How does settlement work?** For each merchant: gross captured − (fee + GST) − unsettled refunds = net payout, recorded as a ledger posting and a Settlement with a UTR. A late refund is netted into the next settlement; negative balances carry forward.

**What testing did you do?** 86 automated tests: validation, auth, idempotency, fraud, refunds, settlement maths, ledger invariants, webhook signing/retries, dashboard pages, CSRF, **parallel-thread race tests**, and live-HTTP end-to-end tests (store → gateway → checkout → webhook).

**What would you add next?** Subscriptions, multi-currency, real PostgreSQL + Celery deployment, 3-D Secure with real bank sandboxes, payouts via a banking API, and chargeback/dispute handling.

## Numbers to remember
2% fee + 18% GST on the fee · 120 requests/min/merchant · webhook retries 10s, 1m, 5m, 30m, 2h · fraud: block ≥ 80, review ≥ 40 · order expiry 24 h · OTP: 3 attempts · 86 tests
