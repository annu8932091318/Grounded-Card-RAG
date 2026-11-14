"""Error codes -> HTTP status + customer-safe message. Internal detail never leaves the server."""

ERRORS = {
    "VALIDATION_ERROR": (400, "The request was not valid."),
    "IDEMPOTENCY_KEY_REQUIRED": (400, "Every POST needs an Idempotency-Key header (8-128 chars: letters, digits, . _ : -)."),
    "IDEMPOTENCY_KEY_INVALID": (400, "Idempotency-Key must be 8-128 chars: letters, digits, . _ : -"),
    "IDEMPOTENCY_KEY_REUSED": (422, "This Idempotency-Key was already used for a different request. Use a new key."),
    "IDEMPOTENCY_IN_PROGRESS": (409, "A request with this Idempotency-Key is still being processed. Retry shortly."),
    "UNAUTHORIZED": (401, "Missing or invalid API key."),
    "FORBIDDEN": (403, "This key cannot use that endpoint."),
    "CUSTOMER_NOT_FOUND": (404, "We couldn't find an account for that customer reference."),
    "NOT_FOUND": (404, "Not found."),
    "RATE_LIMITED": (429, "Too many requests. Please try again shortly."),
    "TIMEOUT": (504, "The request took too long. Nothing has been changed. Please try again."),
    "SERVICE_DEGRADED": (503, "The service is temporarily unavailable. Please try again later."),
    "INTERNAL": (500, "Something went wrong on our side. Nothing has been changed."),
}


class AppError(Exception):
    """Raise anywhere; error_middleware turns it into the safe JSON envelope."""

    def __init__(self, code: str, detail: str | None = None):
        super().__init__(detail or code)
        self.code = code
        self.status, self.message = ERRORS.get(code, ERRORS["INTERNAL"])
