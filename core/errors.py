"""One error shape for every caller (#548, phase A of the service layer #541).

Core raises `AppError(code, message, status=...)` (or a subclass) for anything the caller did
wrong or asked for that can't be done. The two front ends turn it into the same shape:

  * HTTP (web/app.py's handler):  status `exc.status`,
        {"ok": false, "error": {"code", "message"[, "details"]}, "detail": message}
    `detail` stays because the page JS reads it. Plain HTTPExceptions get the same shape with a
    code derived from the status (code_for_status).
  * MCP (mcp_server/server.py's tool wrapper): {"ok": false, "error": {"code", "message"[, "details"]}}

`code` is a stable machine string. Subclasses only pick a default code/status, so callers can
still catch the specific type (decisions.DecisionNotFound, curation_queue.QueueError, ...).
"""

STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "unprocessable",
    429: "too_many_requests",
    500: "internal_error",
    502: "bad_gateway",
    503: "unavailable",
}


def code_for_status(status):
    return STATUS_CODES.get(int(status), f"http_{int(status)}")


class AppError(Exception):
    """A refusal core reports to its caller: a stable `code`, a human `message`, an HTTP
    `status` and optional structured `details`."""

    default_code = "bad_request"
    default_status = 400

    def __init__(self, code=None, message="", status=None, details=None):
        super().__init__(message)
        self.code = code or self.default_code
        self.message = message
        self._status = status
        self.details = details or {}

    @property
    def http_status(self):
        return self._status or self.default_status

    @property
    def status(self):
        return self.http_status

    def to_dict(self):
        return {"code": self.code, "message": self.message, **({"details": self.details} if self.details else {})}

    def __str__(self):
        return self.message


class NotFound(AppError):
    default_code = "not_found"
    default_status = 404

    def __init__(self, message="", code=None, details=None):
        super().__init__(code, message, details=details)


class Conflict(AppError):
    default_code = "conflict"
    default_status = 409

    def __init__(self, message="", code=None, details=None):
        super().__init__(code, message, details=details)


class InvalidInput(AppError, ValueError):
    """User-facing validation failure. Also a ValueError, so existing `except ValueError`
    callers keep working."""

    default_code = "bad_request"
    default_status = 400

    def __init__(self, message="", code=None, details=None, status=None):
        super().__init__(code, message, status=status, details=details)


def error_body(code, message, details=None):
    """The shared {"ok": false, "error": {...}} payload (MCP shape; HTTP adds `detail`)."""
    err = {"code": code, "message": message}
    if details:
        err["details"] = details
    return {"ok": False, "error": err}


def http_body(code, message, details=None, detail=None):
    """The HTTP error body: the shared payload plus `detail` (the page JS reads it).
    `detail` defaults to `message`; an HTTPException's non-string detail is passed through."""
    body = error_body(code, message, details)
    body["detail"] = message if detail is None else detail
    return body


def to_payload(exc):
    """The shared payload for an AppError."""
    return error_body(exc.code, exc.message, exc.details)
