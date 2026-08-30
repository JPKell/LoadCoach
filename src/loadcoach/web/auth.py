"""loadcoach.web.auth — bearer tokens and the cumulative scope rule (api.md §11).

**Scope, deliberately narrow.** Full auth hardening — token management commands, per-token rate
limits, queue-depth caps and the LAN-exposure review — is Phase 9's. What lives here is the one
piece Phase 6 cannot do without: ``POST /evidence/import`` is ``admin``-scoped (spec §14), and a
claim that cannot be enforced is not a claim. Phase 9 extends this module rather than replacing
it.

The rule, from api.md §11:

* **Loopback with no tokens is open.** A single-user install must not need a credential, and this
  is what makes ``loadcoach serve`` work with zero configuration (spec §20 AC1).
* Once any token exists, or the bind is not loopback, a scoped endpoint requires
  ``Authorization: Bearer <token>``.
* Scopes are **cumulative**: ``admin ⊃ write ⊃ read``. A token carries one scope and grants every
  scope beneath it.

A token is stored only as its SHA-256; the comparison is constant-time, and a revoked or expired
row is not a token at all.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING, ClassVar, Final

from baseaicore import SuiteError

from loadcoach.config import LOOPBACK_HOSTS
from loadcoach.infrastructure.db.models import ApiToken

if TYPE_CHECKING:
    from datetime import datetime

    from loadcoach.services.database import Database

__all__ = ["SCOPES", "Forbidden", "Unauthorized", "require_scope", "token_sha256"]

SCOPES: Final[dict[str, int]] = {"read": 0, "write": 1, "admin": 2}
"""api.md §11's three scopes, ordered by what they contain."""


class Unauthorized(SuiteError):
    """A scoped endpoint was called with no usable bearer token."""

    code: ClassVar[str] = "UNAUTHORIZED"


class Forbidden(SuiteError):
    """The token is valid but does not carry the scope this endpoint requires."""

    code: ClassVar[str] = "FORBIDDEN"


def token_sha256(token: str) -> str:
    """Return the stored form of a bearer token.

    Args:
        token: The bearer value.

    Returns:
        Its lowercase hex SHA-256 — the only form ``api_tokens`` ever holds.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def require_scope(
    database: Database,
    *,
    required: str,
    authorization: str | None,
    bind_host: str,
    now: datetime,
) -> str | None:
    """Enforce ``required`` for one request, or raise.

    Args:
        database: The application's database handle.
        required: ``"read"``, ``"write"`` or ``"admin"``.
        authorization: The ``Authorization`` header, or ``None``.
        bind_host: ``server.host`` — what the application is bound to.
        now: The current instant, for expiry.

    Returns:
        The name of the token that authorized the call, or ``None`` when the call was allowed
        because this is an open loopback install.

    Raises:
        Unauthorized: A credential was required and none usable was supplied.
        Forbidden: The token is valid but its scope does not contain ``required``.
    """
    with database.read() as session:
        tokens = session.query(ApiToken).all()
        rows = [
            (row.name, row.token_sha256, row.scope)
            for row in tokens
            if row.revoked_at is None and (row.expires_at is None or row.expires_at > now)
        ]

    presented = _bearer(authorization)
    if not rows:
        if bind_host in LOOPBACK_HOSTS:
            return None
        raise Unauthorized(
            "This endpoint requires a bearer token, and no API token is configured. "
            "A non-loopback bind must have tokens (api.md §11).",
            details={"required_scope": required},
        )

    if presented is None:
        raise Unauthorized(
            f"This endpoint requires a bearer token with the {required!r} scope.",
            details={"required_scope": required},
        )
    digest = token_sha256(presented)
    for name, stored, scope in rows:
        if hmac.compare_digest(stored, digest):
            if SCOPES.get(scope, -1) < SCOPES[required]:
                raise Forbidden(
                    f"This token carries the {scope!r} scope; {required!r} is required. "
                    "Scopes are cumulative: admin contains write contains read.",
                    details={"required_scope": required, "token_scope": scope},
                )
            return name
    raise Unauthorized(
        "The bearer token presented is not a known, active API token.",
        details={"required_scope": required},
    )


def _bearer(authorization: str | None) -> str | None:
    """Extract the bearer value from an ``Authorization`` header, or ``None``."""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()
