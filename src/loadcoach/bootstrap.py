"""loadcoach.bootstrap — the composition root: settings, logging and the ASGI app, wired once.

This module sits outside the ``web``/``cli``/``services``/``domain`` layer ordering that
``.importlinter`` enforces, precisely so it can import both configuration and the web layer.
``loadcoach.cli`` never imports it directly — the ``web-cli-independence`` contract forbids any
import chain from ``cli`` into ``web``, and this module imports ``web``. Instead, the CLI's
``serve`` command hands uvicorn the dotted string
``"loadcoach.bootstrap:create_app_from_environment"`` and lets uvicorn perform that import itself;
a string literal is invisible to import-linter's static analysis, so the two surfaces stay decoupled
at the source level while still running the same application in one process.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import FastAPI
from sqlalchemy import func, select

from loadcoach.config import LOOPBACK_HOSTS, InsecureBindingError, LoadedSettings, load_settings
from loadcoach.infrastructure.db.models import ApiToken
from loadcoach.observability.logging import configure_logging
from loadcoach.services.database import Database, ensure_ready
from loadcoach.web.app import create_app

__all__ = ["Application", "bootstrap", "create_app_from_environment"]


@dataclass(frozen=True, slots=True)
class Application:
    """A fully wired application: the settings it was built from and its ASGI app."""

    loaded_settings: LoadedSettings
    app: FastAPI


def _has_active_token(database: Database) -> bool:
    """Return whether at least one non-revoked, unexpired API token exists.

    The third member of ADR-0026's non-loopback refusal set — bind acknowledgement and
    ``server.allowed_hosts`` are checked in :func:`loadcoach.config.load_settings`, which reads no
    database; this one needs the database, because LoadCoach stores tokens in the ``api_tokens``
    table (created by ``loadcoach token create``) rather than in ``config.toml`` — a token an
    operator can issue and revoke without editing a file.
    """
    now = datetime.now(UTC)
    with database.read() as session:
        count = session.execute(
            select(func.count())
            .select_from(ApiToken)
            .where(
                ApiToken.revoked_at.is_(None),
                (ApiToken.expires_at.is_(None)) | (ApiToken.expires_at > now),
            )
        ).scalar_one()
    return count > 0


def bootstrap() -> Application:
    """Load configuration, configure logging, ready the database, build the app.

    Reads configuration through the standard precedence chain (defaults, file, environment) with
    no CLI-argument layer of its own: a caller that needs CLI overrides applies them as environment
    variables before calling this function, which is what ``loadcoach.cli.commands.system.serve``
    does.

    The startup revision check (database standards §5.1) and the non-loopback token requirement
    (ADR-0026) both run here, in the composition root, and deliberately not inside
    :func:`~loadcoach.web.app.create_app` — that function is documented as a pure function of
    :class:`~loadcoach.config.Settings` precisely so tests can build an app without touching the
    filesystem, and opening a database is neither pure nor free.

    Returns:
        The wired :class:`Application`.

    Raises:
        ConfigurationError: Configuration is invalid, or an unsafe bind combination is configured.
        InsecureBindingError: ``server.host`` is not loopback and no active API token exists.
        MigrationRequired: The database is behind head and ``storage.auto_migrate`` is false.
        SchemaAhead: The database was written by a newer application version.
        DatabaseUnavailable: The configured database could not be reached at all.
    """
    loaded = load_settings()
    configure_logging(
        level=loaded.settings.logging.level, log_format=loaded.settings.logging.format
    )
    database_url = loaded.settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        message = "no database_url configured"
        raise RuntimeError(message)
    with Database.from_url(
        database_url, statement_timeout_ms=loaded.settings.storage.statement_timeout_ms
    ) as database:
        ensure_ready(
            database,
            auto_migrate=loaded.settings.storage.auto_migrate,
            backup_retention=loaded.settings.storage.backup_retention,
        )
        if loaded.settings.server.host not in LOOPBACK_HOSTS and not _has_active_token(database):
            raise InsecureBindingError(
                "server.host is not loopback but no active API token exists. A non-loopback bind "
                "must have at least one token created first: `loadcoach token create`.",
                details={"field": "server.host", "host": loaded.settings.server.host},
            )
    return Application(loaded_settings=loaded, app=create_app(loaded.settings))


def create_app_from_environment() -> FastAPI:
    """Zero-argument ASGI factory: the target uvicorn imports by dotted name.

    See the module docstring for why this is referenced by string rather than imported directly by
    ``loadcoach.cli``.
    """
    return bootstrap().app
