import uuid


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rid = request.META.get("HTTP_X_REQUEST_ID") or "req_" + uuid.uuid4().hex[:12]
        request.request_id = rid
        response = self.get_response(request)
        response["X-Request-Id"] = rid
        return response
