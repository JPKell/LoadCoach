"""loadcoach.services.tokens — API token management (api.md §11, ADR-0026).

The token itself is 256 bits of URL-safe randomness, returned to the caller exactly once; the
row stores its SHA-256 (:func:`~loadcoach.web.auth.token_sha256`'s form, computed here without
importing the web layer). Names are unique among active tokens so ``revoke`` is unambiguous.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar

from baseaicore import SuiteError, ValidationError
from baseaicore.timeutil import to_rfc3339
from sqlalchemy import select

from loadcoach.infrastructure.db.models import ApiToken

if TYPE_CHECKING:
    from datetime import datetime

    from loadcoach.services.database import Database

__all__ = [
    "SCOPES",
    "IssuedToken",
    "TokenNotFound",
    "TokenRecord",
    "create_token",
    "list_tokens",
    "revoke_token",
]

SCOPES = ("read", "write", "admin")


def _stamp(value: datetime | None) -> str | None:
    return None if value is None else to_rfc3339(value)


class TokenNotFound(SuiteError):
    """No active token with that name."""

    code: ClassVar[str] = "TOKEN_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class TokenRecord:
    """One ``api_tokens`` row, without the secret."""

    token_id: str
    name: str
    scope: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None

    @property
    def active(self) -> bool:
        """Whether the token is neither revoked nor known to be expired."""
        return self.revoked_at is None

    def as_json(self) -> dict[str, Any]:
        """The record as ``loadcoach token list --json`` prints it."""
        return {
            "token_id": self.token_id,
            "name": self.name,
            "scope": self.scope,
            "created_at": to_rfc3339(self.created_at),
            "expires_at": _stamp(self.expires_at),
            "revoked_at": _stamp(self.revoked_at),
            "last_used_at": _stamp(self.last_used_at),
        }


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A freshly created token: the record, and the secret shown once."""

    record: TokenRecord
    token: str


def _record(row: ApiToken) -> TokenRecord:
    return TokenRecord(
        token_id=row.id,
        name=row.name,
        scope=row.scope,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
    )


def create_token(
    database: Database, *, name: str, scope: str, expires_days: int | None, now: datetime
) -> IssuedToken:
    """Mint a token and store its digest.

    Raises:
        ValidationError: A blank name, a name already used by an active token, or a scope
            outside ``read``/``write``/``admin``.
    """
    cleaned = name.strip()
    if not cleaned or len(cleaned) > 64:
        raise ValidationError(
            "A token needs a name of 1–64 characters.",
            details={"fields": [{"path": "name", "problem": "1–64 characters, not blank"}]},
        )
    if scope not in SCOPES:
        raise ValidationError(
            f"Scope must be one of {', '.join(SCOPES)}.",
            details={"fields": [{"path": "scope", "problem": "unknown scope"}]},
        )
    secret = secrets.token_urlsafe(32)
    with database.write() as session:
        clash = session.execute(
            select(ApiToken).where(ApiToken.name == cleaned, ApiToken.revoked_at.is_(None))
        ).scalar_one_or_none()
        if clash is not None:
            raise ValidationError(
                f"An active token named {cleaned!r} already exists; revoke it first.",
                details={"fields": [{"path": "name", "problem": "already in use"}]},
            )
        row = ApiToken(
            name=cleaned,
            token_sha256=hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            scope=scope,
            created_at=now,
            expires_at=None if expires_days is None else now + timedelta(days=expires_days),
        )
        session.add(row)
        session.flush()
        record = _record(row)
    return IssuedToken(record=record, token=secret)


def list_tokens(database: Database) -> tuple[TokenRecord, ...]:
    """Every token, newest first, revoked ones included."""
    with database.read() as session:
        rows = session.execute(select(ApiToken).order_by(ApiToken.created_at.desc())).scalars()
        return tuple(_record(row) for row in rows)


def revoke_token(database: Database, *, name: str, now: datetime) -> TokenRecord:
    """Revoke the active token called ``name``.

    Raises:
        TokenNotFound: No active token has that name.
    """
    with database.write() as session:
        row = session.execute(
            select(ApiToken).where(ApiToken.name == name, ApiToken.revoked_at.is_(None))
        ).scalar_one_or_none()
        if row is None:
            raise TokenNotFound(f"No active token named {name!r}.", details={"name": name})
        row.revoked_at = now
        session.flush()
        return _record(row)
