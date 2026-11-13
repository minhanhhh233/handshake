"""FastAPI application factory."""

from __future__ import annotations

import json
import re
import logging
import sys
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..errors import ERROR_CODES, HandshakeError
from ..ids import is_id, new_id

log = logging.getLogger("handshake")

PROBLEM = "application/problem+json"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
        }
        for k in ("trace_id", "path", "status", "duration_ms"):
            if hasattr(record, k):
                d[k] = getattr(record, k)
        return json.dumps(d)


_SECRET_PATH = re.compile(r"^(/approve/)[^/?#]+")


def safe_path(path: str) -> str:
    """Path for logs. Approval tokens live in the path and are redacted; query strings (which
    may carry `?key=` or `?t=`) are never logged (document 08, section 0, phase 3 must-do)."""
    return _SECRET_PATH.sub(r"\1[redacted]", path)


def _configure_logging() -> None:
    root = logging.getLogger("handshake")
    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(_JsonFormatter())
        root.addHandler(h)
        root.setLevel(logging.INFO)
    # uvicorn's access log prints the raw path and query string, which would expose approval
    # tokens and `?key=<ADMIN_KEY>`. Our own request log (path redacted, no query) replaces it.
    logging.getLogger("uvicorn.access").disabled = True


def trace_id_of(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or new_id("trc")


def problem_response(err: HandshakeError, trace_id: str) -> JSONResponse:
    return JSONResponse(
        err.problem(trace_id),
        status_code=err.status,
        media_type=PROBLEM,
        headers={"X-Trace-Id": trace_id},
    )


def create_app() -> FastAPI:
    _configure_logging()
    app = FastAPI(
        title="Handshake",
        version=__version__,
        description=(
            "Agent gateway for Valio Aimo (Junction 2026 prototype). Agents search products, "
            "receive signed fixed-price offers, and place orders within a human-set mandate. "
            "Discovery: /.well-known/handshake.json"
        ),
    )

    @app.middleware("http")
    async def trace_and_log(request: Request, call_next):  # type: ignore[no-untyped-def]
        supplied = request.headers.get("x-trace-id")
        request.state.trace_id = supplied if supplied and is_id(supplied, "trc") else new_id("trc")
        request.state.trace_supplied = bool(supplied and is_id(supplied, "trc"))
        start = time.perf_counter()
        response = await call_next(request)
        # Handlers may switch to another trace (an order inherits its offer's trace).
        response.headers["X-Trace-Id"] = request.state.trace_id
        log.info(
            "request",
            extra={
                "trace_id": request.state.trace_id,
                "path": f"{request.method} {safe_path(request.url.path)}",
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            },
        )
        return response

    @app.exception_handler(HandshakeError)
    async def handshake_error(request: Request, exc: HandshakeError):  # type: ignore[no-untyped-def]
        return problem_response(exc, trace_id_of(request))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):  # type: ignore[no-untyped-def]
        errors = [
            {
                "field": ".".join(str(p) for p in e.get("loc", ()) if p != "body"),
                "message": e.get("msg", "invalid"),
            }
            for e in exc.errors()
        ]
        err = HandshakeError(
            "invalid_request", "Request body or parameters failed validation.",
            extra={"errors": errors},
        )
        return problem_response(err, trace_id_of(request))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):  # type: ignore[no-untyped-def]
        by_status = {v[0]: k for k, v in ERROR_CODES.items()}
        code = by_status.get(exc.status_code, "invalid_request")
        err = HandshakeError(code, str(exc.detail), status=exc.status_code)
        return problem_response(err, trace_id_of(request))

    from . import admin, mandates, offers, orders, products, wellknown

    app.include_router(wellknown.router)
    app.include_router(products.router)
    app.include_router(mandates.router)
    app.include_router(offers.router)
    app.include_router(orders.router)
    app.include_router(admin.router)

    try:
        from ..pages import router as pages_router

        app.include_router(pages_router)
    except ImportError:  # pages arrive in phase 3
        pass

    return app
