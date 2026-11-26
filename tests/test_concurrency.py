"""Race-condition tests: real threads against a file-backed SQLite DB."""
import threading

from django.db import close_old_connections
from django.test import TransactionTestCase

from core.exceptions import GatewayError
from ledger import services as ledger
from ledger.models import LedgerTransaction
from payments import services
from payments.models import Order, Payment, Refund
from .base import make_merchant


def run_parallel(n, fn):
    results, barrier = [None] * n, threading.Barrier(n)
    def worker(i):
        try:
            barrier.wait()
            results[i] = ("ok", fn(i))
        except GatewayError as e:
            results[i] = ("err", e.code)
        except Exception as e:  # any other exception is a bug
            results[i] = ("crash", repr(e))
        finally:
            close_old_connections()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return results


class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.m, *_ = make_merchant()

    def test_parallel_refunds_never_exceed_payment(self):
        o = services.create_order(self.m, 100000)
        p = services.start_payment(o, "upi", {"vpa": "asha@okaxis"}, ip="9.9.9.9")
        res = run_parallel(8, lambda i: services.refund_payment(Payment.objects.get(pk=p.pk), 30000, f"r{i}"))
        self.assertEqual([r for r in res if r[0] == "crash"], [])
        ok = [r for r in res if r[0] == "ok"]
        self.assertEqual(len(ok), 3)                                  # 3 x 30000 = 90000 fits, a 4th would exceed
        p.refresh_from_db()
        self.assertEqual(p.refunded_amount, 90000)
        self.assertEqual(Refund.objects.count(), 3)
        self.assertLessEqual(p.refunded_amount, p.amount)
        self.assertEqual(ledger.verify_ledger(), [])

    def test_parallel_full_refunds_only_one_wins(self):
        o = services.create_order(self.m, 50000)
        p = services.start_payment(o, "upi", {"vpa": "asha@okaxis"}, ip="9.9.9.9")
        res = run_parallel(6, lambda i: services.refund_payment(Payment.objects.get(pk=p.pk), None))
        self.assertEqual([r for r in res if r[0] == "crash"], [])
        p.refresh_from_db()
        self.assertEqual((p.status, p.refunded_amount), ("refunded", 50000))
        self.assertEqual(Refund.objects.count(), 1)

    def test_parallel_payments_on_one_order_capture_once(self):
        o = services.create_order(self.m, 75000)
        res = run_parallel(6, lambda i: services.start_payment(Order.objects.get(pk=o.pk), "upi", {"vpa": f"user{i}@okaxis"}, ip=f"7.7.7.{i}"))
        self.assertEqual([r for r in res if r[0] == "crash"], [])
        self.assertEqual(Payment.objects.filter(status="captured").count(), 1)        # exactly one charge
        self.assertEqual(LedgerTransaction.objects.filter(kind="capture").count(), 1)
        self.assertEqual(Order.objects.get(pk=o.pk).status, "paid")
        self.assertEqual(ledger.merchant_payable(self.m), 75000 - 1500 - 270)
        self.assertEqual(ledger.verify_ledger(), [])

    def test_parallel_settlements_pay_out_once(self):
        for _ in range(3):
            services.start_payment(services.create_order(self.m, 100000), "upi", {"vpa": "asha@okaxis"}, ip="1.2.3.4")
        res = run_parallel(4, lambda i: ledger.run_settlement(self.m))
        self.assertEqual([r for r in res if r[0] == "crash"], [])
        real = [r for r in res if r[0] == "ok" and r[1] is not None]
        self.assertEqual(len(real), 1)
        self.assertEqual(ledger.merchant_payable(self.m), 0)
