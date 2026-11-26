import base64

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import APIKey
from .utils import safe_equal, sha256


class APIKeyAuthentication(BaseAuthentication):
    """HTTP Basic auth where username = key_id and password = secret. Secrets are compared in constant time."""

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.lower().startswith("basic "):
            return None
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
            key_id, secret = decoded.split(":", 1)
        except Exception:
            raise AuthenticationFailed("Malformed credentials")
        try:
            key = APIKey.objects.select_related("merchant__user").get(key_id=key_id)
        except APIKey.DoesNotExist:
            raise AuthenticationFailed("Invalid key")
        if not key.is_active or not safe_equal(key.secret_hash, sha256(secret)):
            raise AuthenticationFailed("Invalid key")
        APIKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
        request.merchant = key.merchant
        return (key.merchant.user, key)

    def authenticate_header(self, request):
        return 'Basic realm="NovaPay"'
