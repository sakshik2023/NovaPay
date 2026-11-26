import os

from django.apps import AppConfig
from django.conf import settings


class WebhooksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "webhooks"

    def ready(self):
        # With the autoreloader, only the child process (RUN_MAIN=true) serves requests.
        if settings.WEBHOOK_WORKER_ENABLED and os.environ.get("RUN_MAIN") == "true":
            from . import worker
            worker.start()
        elif settings.WEBHOOK_WORKER_ENABLED and "--noreload" in __import__("sys").argv:
            from . import worker
            worker.start()
