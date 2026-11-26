from datetime import datetime

from django import template
from django.utils import timezone

from core.utils import format_inr

register = template.Library()


@register.filter
def inr(paise):
    return "₹" + format_inr(paise)


@register.filter
def rupees(paise):
    """Plain number for inputs (e.g. 12345 -> 123.45)"""
    return f"{(paise or 0) / 100:.2f}"


@register.filter
def badge(status):
    return {
        "captured": "ok", "paid": "ok", "delivered": "ok", "processed": "ok", "verified": "ok", "allow": "ok",
        "pending": "warn", "created": "info", "attempted": "info", "retrying": "warn", "review": "warn",
        "partially_refunded": "violet", "refunded": "violet",
        "failed": "bad", "expired": "muted", "block": "bad", "rejected": "bad",
    }.get(status, "muted")


@register.filter
def label(status):
    return (status or "").replace("_", " ").capitalize()


@register.filter
def ts(value):
    """unix timestamp -> aware datetime"""
    return datetime.fromtimestamp(int(value), tz=timezone.get_current_timezone()) if value else None


@register.filter
def initials(name):
    parts = (name or "?").split()
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()


@register.filter
def get_item(d, key):
    return d.get(key) if hasattr(d, "get") else None


@register.simple_tag(takes_context=True)
def qs_replace(context, **kwargs):
    """Rebuild the current query string with some params replaced (for pagination/filters)."""
    q = context["request"].GET.copy()
    for k, v in kwargs.items():
        q[k] = v
    return q.urlencode()


@register.filter
def pretty(value):
    import json
    return json.dumps(value, indent=2, sort_keys=True, default=str)
