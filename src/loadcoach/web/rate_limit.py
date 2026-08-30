"""loadcoach.web.rate_limit — per-token request limits, and a brake on failed authentication.

Spec §14 and api.md §11: per-token rate limits keep one caller from starving the others.
ADR-0014 §6: failed authentication is rate-limited per source address.

**Per token, never per connection.** The bucket is keyed by the presented credential — the
SHA-256 of the bearer (or the UI's token cookie), never the token itself — so a caller opening
ten connections shares one budget and a caller with two tokens has two. A request with no
credential is keyed by its client address, which is what an open loopback install and a stranger
on the LAN both look like before authentication.

**A rate limit that starves a legitimate caller is worse than none.** The bucket is a token
bucket: ``burst`` requests may arrive at once, and the bucket refills at ``per_minute / 60`` per
second. At the boundary the caller gets ``429 RATE_LIMITED`` with a ``Retry-After`` header naming
the seconds until one request is admitted — never a silent drop, never a closed connection.
``per_minute = 0`` disables the limit. Only ``/api/v1`` is limited: a person paging through the
UI is not the failure mode, and ``/api/v1/version`` is exempt because negotiation precedes
credentials (ADR-0026 §5).

Failed authentication: each ``401`` from an address counts; past ``failed_auth_per_minute`` in a
minute the address gets ``429`` for the rest of that minute, whatever it presents. Logged with the
address and request ID, never with the token.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from baseaicore import new_id
from mirrorwall import error_body
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["RateLimitMiddleware", "TokenBucket", "credential_key"]

logger = logging.getLogger(__name__)

_EXEMPT_PATHS = frozenset({"/api/v1/version"})
_API_PREFIX = "/api/v1"


def credential_key(headers: Headers, client_host: str | None) -> tuple[str, bool]:
    """The bucket key for a request: ``(key, authenticated)``.

    A bearer token, or the UI's token cookie, keys by its digest; anything else keys by address.
    """
    authorization = headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    token = value.strip() if scheme.lower() == "bearer" and value.strip() else ""
    if not token:
        for part in headers.get("cookie", "").split(";"):
            name, _, cookie_value = part.strip().partition("=")
            if name == "loadcoach_token" and cookie_value:
                token = cookie_value
                break
    if token:
        return "token:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:24], True
    return "address:" + (client_host or "unknown"), False


@dataclass
class TokenBucket:
    """One caller's budget: ``capacity`` at rest, refilled at ``per_second``."""

    capacity: float
    per_second: float
    tokens: float
    updated_at: float

    def refill(self, now: float) -> float:
        """Apply the refill up to ``now`` and return the tokens available, taking none."""
        elapsed = max(now - self.updated_at, 0.0)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.per_second)
        self.updated_at = now
        return self.tokens

    def wait_seconds(self) -> float:
        """Seconds until one token is available at the current level."""
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.per_second if self.per_second > 0 else math.inf

    def take(self, now: float) -> float:
        """Take one request's worth: ``0.0`` when admitted, else seconds until it would be."""
        if self.refill(now) >= 1.0:
            self.tokens -= 1.0
            return 0.0
        return self.wait_seconds()


class RateLimitMiddleware:
    """ASGI middleware applying the per-credential bucket and the failed-authentication brake."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        per_minute: int,
        burst: int,
        failed_auth_per_minute: int = 20,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wrap ``app``. ``per_minute = 0`` disables the request limit (not the auth brake)."""
        self.app = app
        self._per_minute = max(per_minute, 0)
        self._burst = max(burst, 1)
        self._failed_limit = max(failed_auth_per_minute, 0)
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}
        self._failures: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _bucket(self, key: str) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(
                capacity=float(self._burst),
                per_second=self._per_minute / 60.0,
                tokens=float(self._burst),
                updated_at=self._clock(),
            )
            self._buckets[key] = bucket
            if (
                len(self._buckets) > 10_000
            ):  # a stranger cycling addresses cannot grow this unbounded
                self._buckets.pop(next(iter(self._buckets)))
        return bucket

    def _failure_bucket(self, address: str) -> TokenBucket:
        bucket = self._failures.get(address)
        if bucket is None:
            bucket = TokenBucket(
                capacity=float(self._failed_limit),
                per_second=self._failed_limit / 60.0,
                tokens=float(self._failed_limit),
                updated_at=self._clock(),
            )
            self._failures[address] = bucket
            if len(self._failures) > 10_000:
                self._failures.pop(next(iter(self._failures)))
        return bucket

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Admit, or answer 429 with ``Retry-After``."""
        if scope["type"] != "http" or not scope["path"].startswith(_API_PREFIX):
            await self.app(scope, receive, send)
            return
        if scope["path"] in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        client = scope.get("client")
        address = client[0] if client else None
        key, _authenticated = credential_key(headers, address)
        now = self._clock()
        with self._lock:
            if self._failed_limit and address is not None:
                failures = self._failure_bucket(address)
                if failures.refill(now) < 1.0:  # the budget is spent on each 401, below
                    await self._reject(
                        scope,
                        receive,
                        send,
                        failures.wait_seconds(),
                        "too many failed authentications",
                    )
                    return
            if self._per_minute:
                wait = self._bucket(key).take(now)
                if wait > 0.0:
                    await self._reject(scope, receive, send, wait, "rate limit reached")
                    return

        async def send_wrapper(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and message["status"] == 401
                and self._failed_limit
                and address is not None
            ):
                with self._lock:
                    self._failure_bucket(address).take(self._clock())
                logger.warning("auth.failed", extra={"client": address})
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send, wait: float, reason: str
    ) -> None:
        retry_after = max(1, math.ceil(wait if math.isfinite(wait) else 60.0))
        request_id = scope.get("state", {}).get("request_id") or new_id()
        logger.warning("request.rate_limited", extra={"retry_after": retry_after})
        response = JSONResponse(
            status_code=429,
            content=error_body(
                code="RATE_LIMITED",
                message=f"{reason}; retry after {retry_after} s.",
                request_id=request_id,
                details={"retry_after_seconds": retry_after},
            ),
            headers={
                "Retry-After": str(retry_after),
                "X-Request-ID": request_id,
                "Cache-Control": "no-store",
            },
        )
        await response(scope, receive, send)
