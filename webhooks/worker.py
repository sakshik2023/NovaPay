"""Tiny in-process background worker (replaces Celery so the project runs with zero extra services).
Started automatically with `manage.py runserver`; for production use `manage.py process_webhooks --loop`."""
import threading
import time

from django.conf import settings
from django.db import close_old_connections

_started = {"on": False}


def _loop():
    from .services import process_due
    while True:
        try:
            process_due()
        except Exception:  # never let the worker die
            pass
        finally:
            close_old_connections()
        time.sleep(settings.WEBHOOK_WORKER_INTERVAL)


def start():
    if _started["on"]:
        return
    _started["on"] = True
    threading.Thread(target=_loop, name="webhook-worker", daemon=True).start()
