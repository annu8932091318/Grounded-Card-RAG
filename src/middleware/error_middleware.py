"""Maps every error to { "error": { code, message, request_id } }. Never leaks stack traces or internals."""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.const.errors import ERRORS, AppError
from src.modules.telemetry.logger import log


def error_response(request: Request, code: str) -> JSONResponse:
    status, message = ERRORS.get(code, ERRORS["INTERNAL"])
    request.state.error_code = code  # -> request_logs.error_code
    log("request_error", level="error" if status >= 500 else "warning", code=code, status=status)
    return JSONResponse(status_code=status, content={"error": {
        "code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}})


def register(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return error_response(request, exc.code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        status, message = ERRORS["VALIDATION_ERROR"]
        # field names only (no input echo) so callers can fix the request
        fields = [".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()]
        request.state.error_code = "VALIDATION_ERROR"
        log("request_invalid", level="warning", fields=fields)
        return JSONResponse(status_code=status, content={"error": {
            "code": "VALIDATION_ERROR", "message": message, "fields": fields,
            "request_id": getattr(request.state, "request_id", None)}})

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return error_response(request, "NOT_FOUND" if exc.status_code == 404 else "INTERNAL")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log("unhandled_error", level="error", error=type(exc).__name__, exc_info=(type(exc), exc, exc.__traceback__))
        return error_response(request, "INTERNAL")
