import time

from django.core.cache import cache
from rest_framework.throttling import BaseThrottle
from django.conf import settings


class MerchantRateThrottle(BaseThrottle):
    """Fixed-window per-merchant limiter (cache backed). Exposes remaining quota via headers."""

    def allow_request(self, request, view):
        key = getattr(request, "auth", None)
        if key is None or not hasattr(key, "merchant_id"):
            return True
        rate = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["merchant"]
        limit, per = rate.split("/")
        limit = int(limit)
        window = {"sec": 1, "min": 60, "hour": 3600}[per]
        now = int(time.time())
        bucket = now // window
        ck = f"rl:{key.merchant_id}:{bucket}"
        try:
            count = cache.incr(ck)
        except ValueError:
            cache.set(ck, 1, window + 1)
            count = 1
        self._wait = window - (now % window)
        request.rate_limit = {"limit": limit, "remaining": max(limit - count, 0)}
        return count <= limit

    def wait(self):
        return getattr(self, "_wait", 1)
