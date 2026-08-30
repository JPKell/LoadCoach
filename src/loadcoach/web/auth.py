"""loadcoach.web.auth — resolving the caller (api.md §11, ADR-0014, ADR-0026 §5).

The rule itself — scopes, their order, and the refusal — lives in
:mod:`loadcoach.domain.authorization`; this module only establishes *who* is calling, from the
request, and hands a :class:`~loadcoach.domain.authorization.Principal` to the route and, through
it, to the service. That is the outer half of ADR-0014 §5's "checked in the service layer as well
as at the route".

Where a principal comes from, in order:

1. ``Authorization: Bearer <token>`` — scripts, IdeaPress, the CLI over HTTP.
2. The ``loadcoach_token`` cookie — the same bearer token, carried by a browser, because a page
   navigation cannot add a header. ADR-0014 rejects accounts, passwords and a login subsystem and
   says the bearer token serves the UI too; the cookie is how a server-rendered page receives it.
   It is set from the 401 page by pasting the token once (``POST /token-cookie``), ``HttpOnly``,
   ``SameSite=Strict``, ``Secure``.
3. **Loopback with no tokens is open** (spec §20 AC1): the principal is ``loopback`` with
   ``admin``, and the OS user boundary is the security boundary.

Once any token exists, or the bind is not loopback, every scoped endpoint needs 1 or 2; a token
is stored only as its SHA-256 and compared constant-time; a revoked or expired row is not a token.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, ClassVar, Final

from baseaicore import SuiteError
from fastapi import Depends, Request

from loadcoach.config import LOOPBACK_HOSTS
from loadcoach.domain.authorization import InsufficientScope, Principal, authorize
from loadcoach.infrastructure.db.models import ApiToken

if TYPE_CHECKING:
    from loadcoach.services.database import Database

__all__ = [
    "SCOPES",
    "TOKEN_COOKIE_NAME",
    "CurrentPrincipal",
    "Forbidden",
    "Unauthorized",
    "authenticate",
    "require_scope",
    "resolve_principal",
    "token_sha256",
]

SCOPES: Final[dict[str, int]] = {"read": 0, "write": 1, "admin": 2}
"""api.md §11's three scopes, ordered by what they contain (re-exported from the domain)."""

TOKEN_COOKIE_NAME: Final = "loadcoach_token"  # noqa: S105 — a cookie *name*, not a secret
"""The bearer token, carried by a browser (module docstring, 2)."""

Forbidden = InsufficientScope
"""The token is valid but does not carry the scope this endpoint requires."""


class Unauthorized(SuiteError):
    """A scoped endpoint was called with no usable bearer token."""

    code: ClassVar[str] = "UNAUTHORIZED"


def token_sha256(token: str) -> str:
    """Return the stored form of a bearer token.

    Args:
        token: The bearer value.

    Returns:
        Its lowercase hex SHA-256 — the only form ``api_tokens`` ever holds.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bearer(authorization: str | None) -> str | None:
    """Extract the bearer value from an ``Authorization`` header, or ``None``."""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def resolve_principal(
    database: Database,
    *,
    authorization: str | None,
    bind_host: str,
    now: datetime,
    cookie_token: str | None = None,
) -> Principal:
    """Establish who is calling, or raise.

    Args:
        database: The application's database handle.
        authorization: The ``Authorization`` header, or ``None``.
        bind_host: ``server.host`` — what the application is bound to.
        now: The current instant, for expiry.
        cookie_token: The ``loadcoach_token`` cookie's value, or ``None``.

    Returns:
        The :class:`Principal`: the token's name and scope, or the open loopback install.

    Raises:
        Unauthorized: A credential was required and none usable was supplied.
    """
    with database.read() as session:
        tokens = session.query(ApiToken).all()
        rows = [
            (row.name, row.token_sha256, row.scope)
            for row in tokens
            if row.revoked_at is None and (row.expires_at is None or row.expires_at > now)
        ]
    presented = _bearer(authorization) or (cookie_token.strip() if cookie_token else None)
    if not rows:
        if bind_host in LOOPBACK_HOSTS:
            return Principal(name="loopback", scope="admin", source="loopback")
        raise Unauthorized(
            "This endpoint requires a bearer token, and no API token is configured. "
            "A non-loopback bind must have tokens (api.md §11).",
            details={},
        )
    if presented is None:
        raise Unauthorized("This endpoint requires a bearer token.", details={})
    digest = token_sha256(presented)
    for name, stored, scope in rows:
        if hmac.compare_digest(stored, digest):
            return Principal(name=name, scope=scope, source="token")
    raise Unauthorized("The bearer token presented is not a known, active API token.", details={})


def authenticate(request: Request) -> Principal:
    """The FastAPI dependency every scoped route declares: who is calling this request.

    Records the principal on ``request.state`` so ``source_of`` attributes jobs and feedback to
    the token's name. Never used by ``GET /version`` (ADR-0026 §5).
    """
    settings = request.app.state.settings
    principal = resolve_principal(
        request.app.state.database,
        authorization=request.headers.get("authorization"),
        bind_host=settings.server.host,
        now=datetime.now(UTC),
        cookie_token=request.cookies.get(TOKEN_COOKIE_NAME),
    )
    request.state.principal = principal
    request.state.token_name = principal.name if principal.source == "token" else None
    return principal


CurrentPrincipal = Annotated[Principal, Depends(authenticate)]
"""Declare as a route parameter; then ``authorize(principal, "<scope>")`` is the route's check."""


def require_scope(
    database: Database,
    *,
    required: str,
    authorization: str | None,
    bind_host: str,
    now: datetime,
    cookie_token: str | None = None,
) -> str | None:
    """Enforce ``required`` for one request, or raise — the P6 entry point, kept for its callers.

    Returns:
        The name of the token that authorized the call, or ``None`` when the call was allowed
        because this is an open loopback install.

    Raises:
        Unauthorized: A credential was required and none usable was supplied.
        Forbidden: The token is valid but its scope does not contain ``required``.
    """
    principal = resolve_principal(
        database,
        authorization=authorization,
        bind_host=bind_host,
        now=now,
        cookie_token=cookie_token,
    )
    authorize(principal, required)
    return principal.name if principal.source == "token" else None
