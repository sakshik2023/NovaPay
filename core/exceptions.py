"""One error format for the whole API:  {"error": {"code", "description", "field"?}}"""
from rest_framework import status
from rest_framework.exceptions import APIException, NotAuthenticated, AuthenticationFailed, Throttled, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler


class GatewayError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "BAD_REQUEST_ERROR"

    def __init__(self, description, code=None, field=None, status_code=None):
        super().__init__(detail=description)
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.field = field
        self.description = description


class InvalidTransition(GatewayError):
    status_code = 409

    def __init__(self, current, new):
        super().__init__(f"Cannot move payment from '{current}' to '{new}'", code="INVALID_STATE")


def _err(code, description, http, field=None, headers=None):
    body = {"error": {"code": code, "description": description}}
    if field:
        body["error"]["field"] = field
    return Response(body, status=http, headers=headers)


def gateway_exception_handler(exc, context):
    if isinstance(exc, GatewayError):
        return _err(exc.code, exc.description, exc.status_code, exc.field)
    if isinstance(exc, ValidationError):
        detail = exc.detail
        if isinstance(detail, dict) and detail:
            field, msgs = next(iter(detail.items()))
            msg = msgs[0] if isinstance(msgs, list) and msgs else msgs
            return _err("BAD_REQUEST_ERROR", f"{field}: {msg}", 400, field=field)
        return _err("BAD_REQUEST_ERROR", str(detail), 400)
    if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
        return _err("AUTHENTICATION_ERROR", "Invalid or missing API credentials", 401,
                    headers={"WWW-Authenticate": 'Basic realm="NovaPay"'})
    if isinstance(exc, Throttled):
        return _err("RATE_LIMITED", f"Too many requests. Retry in {int(exc.wait or 1)}s", 429,
                    headers={"Retry-After": str(int(exc.wait or 1))})
    resp = exception_handler(exc, context)
    if resp is not None:
        return _err("API_ERROR", str(getattr(exc, "detail", exc)), resp.status_code)
    return None
