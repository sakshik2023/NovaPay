"""Idempotency-Key support for write endpoints.

  new key             -> run handler, store response
  same key, same body -> replay stored response (header Idempotent-Replayed: true)
  same key, diff body -> 422 IDEMPOTENCY_KEY_REUSED
  same key in flight  -> 409 (response not stored yet)
"""
import json

from django.db import IntegrityError
from rest_framework.response import Response

from .exceptions import GatewayError
from .models import IdempotencyRecord
from .utils import sha256


def run_idempotent(request, endpoint, handler):
    key = request.META.get("HTTP_IDEMPOTENCY_KEY")
    if not key:
        return handler()
    if len(key) > 120:
        raise GatewayError("Idempotency-Key too long (max 120)", code="BAD_REQUEST_ERROR")
    body_hash = sha256(json.dumps(request.data, sort_keys=True, default=str) + endpoint)
    merchant = request.merchant

    try:
        record, created = IdempotencyRecord.objects.get_or_create(
            merchant=merchant, key=key, defaults={"endpoint": endpoint, "request_hash": body_hash}
        )
    except IntegrityError:  # concurrent first insert
        record, created = IdempotencyRecord.objects.get(merchant=merchant, key=key), False

    if not created:
        if record.request_hash != body_hash:
            raise GatewayError("Idempotency-Key was already used with a different request",
                               code="IDEMPOTENCY_KEY_REUSED", status_code=422)
        if record.response_status == 0:
            raise GatewayError("A request with this Idempotency-Key is still in progress",
                               code="REQUEST_IN_PROGRESS", status_code=409)
        resp = Response(record.response_body, status=record.response_status)
        resp["Idempotent-Replayed"] = "true"
        return resp

    try:
        resp = handler()
    except Exception:
        record.delete()  # allow a clean retry after a failed attempt
        raise
    record.response_status = resp.status_code
    record.response_body = resp.data
    record.save(update_fields=["response_status", "response_body"])
    return resp
