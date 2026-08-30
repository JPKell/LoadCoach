"""loadcoach.web.app — the FastAPI application factory.

``create_app`` is a pure function of :class:`~loadcoach.config.Settings`, so tests can build an app
without touching environment variables or the filesystem — the database handle is created by the
lifespan, which runs only when the application is actually served.

Host validation and request-ID middleware come from MirrorWall (that package's Phase 2), not from
this module: three implementations of one security control are three chances to get it subtly
different, and the difference will be in the application nobody audited (ADR-0026 §1). The error
envelope likewise comes from `mirrorwall.error_body`, so LoadCoach's bodies are byte-identical to
every other application's by construction rather than by review.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final

from baseaicore import SuiteError, new_id
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mirrorwall import (
    HostValidationMiddleware,
    RequestIdMiddleware,
    error_body,
    loopback_allowlist,
    mount_static,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from loadcoach.__about__ import __version__
from loadcoach.config import LOOPBACK_HOSTS, Settings
from loadcoach.infrastructure.providers.factory import build_provider
from loadcoach.services.database import Database
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.worker import build_runtime
from loadcoach.web.rendering import templates
from loadcoach.web.routes import evidence as evidence_routes
from loadcoach.web.routes import generate as generate_routes
from loadcoach.web.routes import jobs as jobs_routes
from loadcoach.web.routes import models as models_routes
from loadcoach.web.routes import queue as queue_routes
from loadcoach.web.routes import routing as routing_routes
from loadcoach.web.routes import system as system_routes
from loadcoach.web.routes import task_profiles as task_profiles_routes
from loadcoach.web.routing_support import current_snapshot

__all__ = ["create_app"]

logger = logging.getLogger(__name__)

_REQUEST_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_STATUS_BY_CODE: dict[str, int] = {
    "VALIDATION_ERROR": status.HTTP_400_BAD_REQUEST,
    "CONFIGURATION_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "INSECURE_BINDING": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "MISDIRECTED_REQUEST": 421,
    "TASK_PROFILE_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "NO_ELIGIBLE_MODEL": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "ALL_CANDIDATES_FAILED": status.HTTP_502_BAD_GATEWAY,
    "PROVIDER_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "PROVIDER_TIMEOUT": status.HTTP_504_GATEWAY_TIMEOUT,
    "PROVIDER_PROTOCOL_ERROR": status.HTTP_502_BAD_GATEWAY,
    "MODEL_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "CONTEXT_LIMIT_EXCEEDED": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "CAPABILITY_UNSUPPORTED": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "INSUFFICIENT_RESOURCES": status.HTTP_503_SERVICE_UNAVAILABLE,
    "QUEUE_FULL": status.HTTP_429_TOO_MANY_REQUESTS,
    "JOB_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "JOB_NOT_CANCELLABLE": status.HTTP_409_CONFLICT,
    "TRANSITION_REFUSED": status.HTTP_409_CONFLICT,
    "ILLEGAL_TRANSITION": status.HTTP_409_CONFLICT,
    "MAX_WAIT_EXCEEDED": status.HTTP_504_GATEWAY_TIMEOUT,
    "ATTEMPT_REFUSED": status.HTTP_409_CONFLICT,
    "DATABASE_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "DATABASE_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "MIGRATION_REQUIRED": status.HTTP_503_SERVICE_UNAVAILABLE,
    "MIGRATION_FAILED": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "SCHEMA_AHEAD": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "STORAGE_BUSY": status.HTTP_503_SERVICE_UNAVAILABLE,
    "STORAGE_FULL": status.HTTP_507_INSUFFICIENT_STORAGE,
    "INTERNAL_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
    # Phase 6. api.md §10 lists no HTTP status for these three; chosen so that a caller can
    # tell "your bundle is wrong" (422) from "I will not fetch that" (403).
    "EVIDENCE_IMPORT_FAILED": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "SCHEMA_VERSION_UNSUPPORTED": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "EVIDENCE_SOURCE_REFUSED": status.HTTP_403_FORBIDDEN,
    "UNAUTHORIZED": status.HTTP_401_UNAUTHORIZED,
    "FORBIDDEN": status.HTTP_403_FORBIDDEN,
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
    return JSONResponse(
        status_code=status_code,
        content=error_body(code=code, message=message, request_id=request_id, details=details),
        headers={"X-Request-ID": request_id},
    )


def _resolve_allowed_hosts(settings: Settings) -> frozenset[str]:
    """The Host-header allowlist for this bind (ADR-0026 §1)."""
    host = settings.server.host.lower()
    if host in LOOPBACK_HOSTS:
        return loopback_allowlist(host)
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
    app.state.event_sink = JobEventSink()
    # The queue runtime: max_concurrent_jobs worker threads and the scheduler thread with the
    # lease keeper (queue §3, ADR-0029 §4). Started here because the lifespan is where the
    # process commits to serving; tests that enter TestClient get the real threads too.
    runtime = build_runtime(
        settings,
        database=database,
        provider=app.state.provider,
        sink=app.state.event_sink,
        snapshot=lambda: current_snapshot(app),
    )
    app.state.queue_runtime = runtime
    runtime.start()
    try:
        yield
    finally:
        runtime.stop()
        database.close()
        app.state.queue_runtime = None
        app.state.database = None
        app.state.provider = None


def create_app(settings: Settings) -> FastAPI:
    """Build the FastAPI application for the given settings.

    Registers, from outermost to innermost: MirrorWall's request-ID middleware, its Host-header
    validation, the standard error envelope handlers, the ``/api/v1`` routes (system, models,
    task-profiles, routing, generation, jobs, queue), the HTML pages at ``/models``,
    ``/task-profiles``, ``/routing``, ``/jobs``, ``/queue`` and ``/evidence``, and MirrorWall's
    static assets.

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
    app.state.telemetry_collector = None
    app.state.event_sink = None
    app.state.queue_runtime = None

    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(HostValidationMiddleware, allowed_hosts=_resolve_allowed_hosts(settings))

    register_exception_handlers(app)

    app.include_router(system_routes.router, prefix="/api/v1")
    app.include_router(models_routes.router, prefix="/api/v1")
    app.include_router(task_profiles_routes.router, prefix="/api/v1")
    app.include_router(routing_routes.router, prefix="/api/v1")
    app.include_router(generate_routes.router, prefix="/api/v1")
    app.include_router(jobs_routes.router, prefix="/api/v1")
    app.include_router(queue_routes.router, prefix="/api/v1")
    app.include_router(evidence_routes.router, prefix="/api/v1")
    app.include_router(models_routes.ui_router)
    app.include_router(task_profiles_routes.ui_router)
    app.include_router(routing_routes.ui_router)
    app.include_router(jobs_routes.ui_router)
    app.include_router(queue_routes.ui_router)
    app.include_router(evidence_routes.ui_router)

    # MirrorWall's own assets, served from the installed package: no CDN, no network request at
    # page load. Passing the environment swaps the plain `asset_url` filter for the hashing one,
    # so every template starts emitting cacheable URLs without a single template change.
    mount_static(app, environment=templates())

    return app
