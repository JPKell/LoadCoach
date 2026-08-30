"""loadcoach.domain.authorization — who is calling, and the cumulative scope rule (api.md §11).

ADR-0014 §5: scopes are checked **in the service layer as well as at the route**. This module is
the one rule both layers call, so they cannot disagree: a :class:`Principal` names the caller and
the scope it holds, and :func:`authorize` refuses when that scope does not contain the required
one. ``admin ⊃ write ⊃ read``.

The route resolves the principal from the request (a bearer token, the UI's token cookie, or the
open loopback install) and checks first — the *outer* check, so an unauthorized call never
reaches a service. Every mutating service takes the principal and checks again — the *inner*
check, so a future internal caller that reaches the service directly with a read-scoped principal
is refused too. That second check is the one P9 names as its likely failure mode when missing.

Pure: no framework, no database. A ``Principal`` is a value, and ``authorize`` is a function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final, Literal

from baseaicore import SuiteError

__all__ = [
    "LOCAL",
    "SCOPES",
    "InsufficientScope",
    "Principal",
    "Scope",
    "authorize",
]

type Scope = Literal["read", "write", "admin"]

SCOPES: Final[dict[str, int]] = {"read": 0, "write": 1, "admin": 2}
"""api.md §11's three scopes, ordered by what they contain."""


class InsufficientScope(SuiteError):
    """The caller is known, and its scope does not contain the one this operation requires."""

    code: ClassVar[str] = "FORBIDDEN"


@dataclass(frozen=True, slots=True)
class Principal:
    """The caller of one operation.

    Attributes:
        name: The token's name, ``"loopback"`` for an open single-user install, or ``"local"``
            for the process's own CLI. Also the ``source`` a job or feedback record is attributed
            to.
        scope: The one scope the caller holds; it grants every scope beneath it.
        source: How the principal was established — ``"token"`` (a bearer or the UI's token
            cookie), ``"loopback"`` (no tokens configured, loopback bind), or ``"internal"``
            (the CLI or the worker, which run as the operator on the machine).
    """

    name: str
    scope: str
    source: Literal["token", "loopback", "internal"]

    def grants(self, required: str) -> bool:
        """Whether this principal's scope contains ``required``."""
        return SCOPES.get(self.scope, -1) >= SCOPES.get(required, 99)


LOCAL: Final = Principal(name="local", scope="admin", source="internal")
"""The process itself: ``loadcoach`` commands run by the operator on the machine, and the queue's
own workers. Admin, because the OS user boundary is the security boundary there (ADR-0014)."""


def authorize(principal: Principal | None, required: str) -> Principal | None:
    """Refuse unless ``principal`` holds ``required`` (or a scope containing it).

    ``None`` — no principal at all — is allowed through: it is what a caller inside the process
    passes when there is no request to attribute the call to (a scheduler sweep, a test of the
    arithmetic). The web layer never passes ``None``; the contract test over the route table
    holds it to that.

    Args:
        principal: The caller, or ``None`` for an internal call with no request behind it.
        required: ``"read"``, ``"write"`` or ``"admin"``.

    Returns:
        The principal, for chaining.

    Raises:
        InsufficientScope: The principal's scope does not contain ``required``.
        ValueError: ``required`` is not a scope.
    """
    if required not in SCOPES:
        message = f"unknown scope {required!r}; expected one of {sorted(SCOPES)}"
        raise ValueError(message)
    if principal is None or principal.grants(required):
        return principal
    raise InsufficientScope(
        f"{principal.name!r} holds the {principal.scope!r} scope; {required!r} is required. "
        "Scopes are cumulative: admin contains write contains read.",
        details={
            "required_scope": required,
            "token_scope": principal.scope,
            "principal": principal.name,
        },
    )
