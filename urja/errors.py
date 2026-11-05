"""RFC 9457 ``application/problem+json`` error responses.

One error shape everywhere. Notably, "no such meter" (404) is kept distinct
from "the portal is unreachable" (503): a consumer must be able to tell a
permanent absence from a temporary outage and retry accordingly.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_CONTENT_TYPE = "application/problem+json"


class Problem(Exception):
    def __init__(
        self,
        status: int,
        title: str,
        detail: str,
        *,
        type_: str = "about:blank",
        extra: dict | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail
        self.type_ = type_
        self.extra = extra or {}

    def body(self, request: Request) -> dict:
        payload = {
            "type": self.type_,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "instance": request.url.path,
        }
        payload.update(self.extra)
        return payload


def not_found(what: str, identifier: str) -> Problem:
    return Problem(404, "Not found", f"No {what} with id '{identifier}' exists in the indexed data.")


def portal_unavailable(detail: str) -> Problem:
    return Problem(
        503,
        "Portal unavailable",
        detail,
        type_="urn:urja:portal-unavailable",
        extra={"retryable": True, "retryAfterSeconds": 30},
    )


def register(app) -> None:  # noqa: ANN001 - FastAPI instance
    @app.exception_handler(Problem)
    async def _problem(request: Request, exc: Problem) -> JSONResponse:
        headers = {"cache-control": "no-store"}
        if exc.status == 503:
            headers["retry-after"] = str(exc.extra.get("retryAfterSeconds", 30))
        return JSONResponse(
            exc.body(request), status_code=exc.status, headers=headers, media_type=PROBLEM_CONTENT_TYPE
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Covers what we never raise ourselves: unmatched routes, 405s, and any
        # HTTPException from a dependency. Without this, those responses are
        # plain `{"detail": ...}` JSON and break the "one error shape" promise.
        phrase = exc.detail if isinstance(exc.detail, str) else "Request failed"
        problem = Problem(
            exc.status_code,
            phrase,
            f"{request.method} {request.url.path} could not be served.",
        )
        return JSONResponse(
            problem.body(request),
            status_code=exc.status_code,
            media_type=PROBLEM_CONTENT_TYPE,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        problem = Problem(
            422,
            "Invalid request",
            "One or more query parameters or path segments are invalid.",
            type_="urn:urja:validation",
            extra={
                "errors": [
                    {
                        "location": ".".join(str(part) for part in error.get("loc", ())),
                        "message": error.get("msg", "invalid value"),
                    }
                    for error in exc.errors()
                ]
            },
        )
        return JSONResponse(
            problem.body(request), status_code=422, media_type=PROBLEM_CONTENT_TYPE
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Anything unexpected becomes a problem+json 500 rather than an HTML
        # traceback, so a programmatic client can always parse the failure.
        problem = Problem(
            500,
            "Internal error",
            "The service failed to handle this request; the detail is in the service log.",
        )
        return JSONResponse(
            problem.body(request), status_code=500, media_type=PROBLEM_CONTENT_TYPE
        )
