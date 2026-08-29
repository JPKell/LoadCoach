"""loadcoach.web.app — the FastAPI application factory.

``create_app`` is a pure function of :class:`~loadcoach.config.Settings`, so tests can build an app
without touching environment variables or the filesystem — the database handle is created by the
lifespan, which runs only when the application is actually served.

Host validation and request-ID middleware are defined here rather than in MirrorWall, which does
not exist until Phase 4's extraction (ADR-0011); FreeWeight built the identical pair in-application
before its own extraction, and this is the same move at the same point in the sequence.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final

from baseaicore import SuiteError, new_id
from baseaicore.timeutil import to_rfc3339, utc_now
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from loadcoach.__about__ import __version__
from loadcoach.config import LOOPBACK_HOSTS, Settings
from loadcoach.infrastructure.providers.factory import build_provider
from loadcoach.observability.logging import bind_context
from loadcoach.services.database import Database
from loadcoach.web.routes import models as models_routes
from loadcoach.web.routes import system as system_routes
from loadcoach.web.routes import task_profiles as task_profiles_routes

__all__ = ["create_app"]

logger = logging.getLogger(__name__)

_REQUEST_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_STATUS_BY_CODE: dict[str, int] = {
    "VALIDATION_ERROR": status.HTTP_400_BAD_REQUEST,
    "CONFIGURATION_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "INSECURE_BINDING": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "MISDIRECTED_REQUEST": 421,
    "DATABASE_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "DATABASE_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "MIGRATION_REQUIRED": status.HTTP_503_SERVICE_UNAVAILABLE,
    "MIGRATION_FAILED": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "SCHEMA_AHEAD": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "STORAGE_BUSY": status.HTTP_503_SERVICE_UNAVAILABLE,
    "STORAGE_FULL": status.HTTP_507_INSUFFICIENT_STORAGE,
    "INTERNAL_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
}

_CODE_BY_HTTP_STATUS: dict[int, str] = {
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    421: "MISDIRECTED_REQUEST",
}


class ErrorDetail(BaseModel):
    """The inner ``error`` object, identical across every application in the suite (API §4)."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str
    timestamp: str


class ErrorEnvelope(BaseModel):
    """The complete error response body: ``{"error": {...}}``, never further wrapped."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail


def _request_id_of(request: Request) -> str:
    """Return this request's ID, generating one if the request-ID middleware did not run."""
    state_id = getattr(request.state, "request_id", None)
    return state_id if isinstance(state_id, str) and state_id else new_id()


def _error_response(
    *,
    request_id: str,
    code: str,
    message: str,
    status_code: int,
    details: Mapping[str, Any] | None = None,
) -> JSONResponse:
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=code,
            message=message,
            details=dict(details or {}),
            request_id=request_id,
            timestamp=to_rfc3339(utc_now()),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers={"X-Request-ID": request_id},
    )


def _split_host(header: str) -> str:
    """Extract the hostname from a ``Host`` header, handling bracketed IPv6 literals."""
    header = header.strip()
    if header.startswith("["):
        end = header.find("]")
        if end != -1:
            return header[1:end].lower()
    return header.split(":", 1)[0].lower()


class RequestIdMiddleware:
    """Assigns or echoes a request ID; binds it into ``request.state`` for the request."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Assign or echo the request ID and add it to the response headers."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        supplied = headers.get("x-request-id")
        request_id = supplied if supplied and _REQUEST_ID_PATTERN.match(supplied) else new_id()
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                mutable_headers = MutableHeaders(raw=list(message["headers"]))
                mutable_headers["X-Request-ID"] = request_id
                mutable_headers["X-Api-Version"] = "v1"
                if "cache-control" not in mutable_headers:
                    mutable_headers["Cache-Control"] = "no-store"
                message["headers"] = mutable_headers.raw
            await send(message)

        with bind_context(request_id=request_id):
            await self.app(scope, receive, send_wrapper)


class HostValidationMiddleware:
    """Rejects any request whose ``Host`` header is not on the allowlist (ADR-0026 §1)."""

    def __init__(self, app: ASGIApp, *, allowed_hosts: frozenset[str]) -> None:
        """Wrap ``app``, accepting only requests whose ``Host`` header is in ``allowed_hosts``."""
        self.app = app
        self._allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject a mismatched ``Host`` header with 421 before the request reaches routing."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        host_header = headers.get("host", "")
        host = _split_host(host_header)
        if host not in self._allowed_hosts:
            logger.warning("request.host_rejected", extra={"host": host_header})
            request_id = scope.get("state", {}).get("request_id") or new_id()
            response = _error_response(
                request_id=request_id,
                code="MISDIRECTED_REQUEST",
                message="The Host header does not match an allowed hostname for this server.",
                status_code=421,
                details={"host": host_header},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _resolve_allowed_hosts(settings: Settings) -> frozenset[str]:
    """The Host-header allowlist for this bind (ADR-0026 §1)."""
    host = settings.server.host.lower()
    if host in LOOPBACK_HOSTS:
        return frozenset({"localhost", "127.0.0.1", "::1", host})
    return frozenset(name.lower() for name in settings.server.allowed_hosts) | {host}


def _docs_allowed(settings: Settings) -> bool:
    """Interactive API docs are loopback-only by default (API standards §11)."""
    return settings.server.host in LOOPBACK_HOSTS


def register_exception_handlers(app: FastAPI) -> None:
    """Register the handlers that translate every exception type into the standard envelope."""

    @app.exception_handler(SuiteError)
    async def _suite_error_handler(request: Request, exc: SuiteError) -> JSONResponse:
        status_code = _STATUS_BY_CODE.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        if status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.error("request.failed", extra={"code": exc.code}, exc_info=exc)
        else:
            logger.warning("request.rejected", extra={"code": exc.code})
        return _error_response(
            request_id=_request_id_of(request),
            code=exc.code,
            message=exc.message,
            status_code=status_code,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = [
            {
                "path": ".".join(str(part) for part in error["loc"] if part != "body"),
                "problem": error["msg"],
            }
            for error in exc.errors()
        ]
        return _error_response(
            request_id=_request_id_of(request),
            code="VALIDATION_ERROR",
            message="Request body failed validation.",
            status_code=status.HTTP_400_BAD_REQUEST,
            details={"fields": fields},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _CODE_BY_HTTP_STATUS.get(exc.status_code, "HTTP_ERROR")
        message = exc.detail if isinstance(exc.detail, str) and exc.detail else "Request failed."
        return _error_response(
            request_id=_request_id_of(request),
            code=code,
            message=message,
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("request.unhandled_error", exc_info=exc)
        return _error_response(
            request_id=_request_id_of(request),
            code="INTERNAL_ERROR",
            message="An unexpected error occurred.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own one database handle and one provider handle for as long as the server serves.

    An engine is a connection pool plus SQLAlchemy's compiled-statement cache; building one per
    request throws both away every time. The handle is created here rather than in
    :func:`create_app` so that it is disposed when the server stops, and so that building an app
    object (which tests do freely) opens nothing.
    """
    settings: Settings = app.state.settings
    database_url = settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        message = "no database_url configured"
        raise RuntimeError(message)
    database = Database.from_url(
        database_url, statement_timeout_ms=settings.storage.statement_timeout_ms
    )
    app.state.database = database
    app.state.provider = build_provider(settings.provider)
    try:
        yield
    finally:
        database.close()
        app.state.database = None
        app.state.provider = None


def create_app(settings: Settings) -> FastAPI:
    """Build the FastAPI application for the given settings.

    Registers, from outermost to innermost: the request-ID middleware, Host-header validation, the
    standard error envelope handlers, the ``/api/v1`` routes (system, models, task-profiles), and
    the plain (pre-MirrorWall) HTML pages at ``/models`` and ``/task-profiles``.

    Still a pure function of its arguments — it opens nothing; the database and provider handles
    are created by the lifespan, which runs only when the application is actually served (or when
    a test enters ``TestClient`` as a context manager).
    """
    app = FastAPI(
        title="LoadCoach",
        version=__version__,
        docs_url="/api/v1/docs" if _docs_allowed(settings) else None,
        openapi_url="/api/v1/openapi.json" if _docs_allowed(settings) else None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.database = None
    app.state.provider = None

    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(HostValidationMiddleware, allowed_hosts=_resolve_allowed_hosts(settings))

    register_exception_handlers(app)

    app.include_router(system_routes.router, prefix="/api/v1")
    app.include_router(models_routes.router, prefix="/api/v1")
    app.include_router(task_profiles_routes.router, prefix="/api/v1")
    app.include_router(models_routes.ui_router)
    app.include_router(task_profiles_routes.ui_router)

    return app
