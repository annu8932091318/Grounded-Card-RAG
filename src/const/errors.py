"""Error codes -> HTTP status + customer-safe message. Internal detail never leaves the server."""

ERRORS = {
    "VALIDATION_ERROR": (400, "The request was not valid."),
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
