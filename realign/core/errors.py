"""Domain error taxonomy + FastAPI exception handlers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from structlog.stdlib import BoundLogger

from realign.core.logging import get_logger

log: BoundLogger = get_logger("realign.errors")


class ReAlignError(Exception):
    """Base class for every expected, user-facing error."""

    status_code: int = 400
    code: str = "error"

    def __init__(
        self,
        message: str = "حدث خطأ",
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details is not None:
            payload["error"]["details"] = self.details
        return payload


class ValidationError(ReAlignError):
    status_code = 422
    code = "validation_error"


class AuthenticationError(ReAlignError):
    status_code = 401
    code = "unauthenticated"

    def __init__(self, message: str = "يجب تسجيل الدخول") -> None:
        super().__init__(message)


class PermissionDenied(ReAlignError):
    status_code = 403
    code = "forbidden"

    def __init__(self, message: str = "ليس لديك صلاحية للقيام بهذا الإجراء") -> None:
        super().__init__(message)


class NotFound(ReAlignError):
    status_code = 404
    code = "not_found"

    def __init__(self, message: str = "العنصر غير موجود") -> None:
        super().__init__(message)


class Conflict(ReAlignError):
    status_code = 409
    code = "conflict"


class RateLimited(ReAlignError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str = "تم تجاوز عدد الطلبات المسموح") -> None:
        super().__init__(message)


# ------------------------------------------------------------- billing -----


class InsufficientCredits(ReAlignError):
    status_code = 402
    code = "insufficient_credits"

    def __init__(self, message: str = "الرصيد غير كافٍ", *, required: int = 0, available: int = 0) -> None:
        super().__init__(message, details={"required": required, "available": available})


class BudgetExceeded(ReAlignError):
    status_code = 402
    code = "budget_exceeded"


class KillSwitchEngaged(ReAlignError):
    status_code = 423
    code = "kill_switch_engaged"

    def __init__(self, message: str = "تم إيقاف التشغيل مؤقتًا (Kill Switch)") -> None:
        super().__init__(message)


# --------------------------------------------------------- integrations ----


class IntegrationError(ReAlignError):
    status_code = 502
    code = "integration_error"


class IntegrationNotConfigured(IntegrationError):
    status_code = 424
    code = "integration_not_configured"

    def __init__(self, connector: str = "", message: str | None = None) -> None:
        super().__init__(
            message or f"الربط '{connector}' غير مُعد بعد. أضف بيانات الاعتماد من صفحة الربطات.",
            details={"connector": connector},
        )


class LLMError(ReAlignError):
    status_code = 502
    code = "llm_error"


def _wants_html(request: Request) -> bool:
    """True when the caller is a browser page, not an API client."""
    accept = (request.headers.get("accept") or "").lower()
    path = request.url.path
    return "text/html" in accept and not path.startswith("/api/")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ReAlignError)
    async def _realign_error(request: Request, exc: ReAlignError):
        log.warning("realign_error", code=exc.code, message=exc.message, path=request.url.path)
        # A browser hitting a web route gets bounced back with a readable
        # Arabic error instead of a bare JSON payload.
        if _wants_html(request) and exc.status_code in (401, 403):
            from starlette.responses import RedirectResponse

            if exc.code == "forbidden":
                return RedirectResponse(url="/login?error=csrf", status_code=303)
            return RedirectResponse(url="/login", status_code=303)
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "بيانات غير صحيحة",
                    "details": [
                        {"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]}
                        for e in exc.errors()
                    ],
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception", path=request.url.path)
        message = "حدث خطأ غير متوقع" if True else str(exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": message}},
        )
