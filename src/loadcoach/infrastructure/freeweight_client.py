"""loadcoach.infrastructure.freeweight_client — the optional pull from FreeWeight.

A thin client over two public HTTP endpoints, and the fetch allowlist that guards them
([ADR-0026 §3](../../docs/adr/0026-local-http-hardening.md)). LoadCoach never imports
``freeweight``, never opens its database, and never copies code out of it: what crosses the
boundary is one versioned SetSpec document over HTTP, exactly as the architecture says.

**Every refusal happens before a single byte of the body is interpreted.** Scheme, host
allowlist, literal and resolved link-local addresses and the redirect rules are decided from the
URL alone; ``Content-Type`` and the size cap are decided from the response head and from the byte
count as it streams. A body that is not even JSON is therefore refused with
``EVIDENCE_SOURCE_REFUSED`` and not with a parse error — which is what makes "checked before
parsing" a testable claim rather than a comment.

The size cap is **streaming**, not a check made after the read. A hostile or broken server that
answers a 2 KiB request with an endless body must cost bounded memory, and a limit applied to an
already-materialized response would have cost all of it before deciding.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final

import httpx
from baseaicore import SuiteError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from loadcoach.config import EvidenceSettings

__all__ = [
    "ACCEPTED_MEDIA_TYPES",
    "ALLOWED_SCHEMES",
    "MAX_IMPORT_BYTES",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "EVIDENCE_COLLECTION_PATH",
    "EVIDENCE_EXPORT_PATH",
    "MAX_REDIRECTS",
    "EvidenceSourceRefused",
    "EvidenceSourceUnreachable",
    "FetchPolicy",
    "FetchedBundle",
    "FreeWeightClient",
    "check_url",
    "policy_from_settings",
    "resolve_credential",
]

MAX_IMPORT_BYTES: Final[int] = 128 * 1024 * 1024
"""ADR-0026 §3's import limit, enforced **during streaming**.

A transfer cap: it bounds what LoadCoach will pull from a stranger before it knows what the body
is. It is deliberately larger than :data:`~loadcoach.services.evidence.MAX_PARSE_BYTES`, which
bounds what may then be turned into objects — see that constant for why the two differ.
"""

ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
"""ADR-0026 §3: nothing else. ``file://`` in particular is a local-file read wearing a URL."""

MAX_REDIRECTS: Final[int] = 3
"""ADR-0026 §3's cap. A redirect that changes host is refused whatever the count."""

DEFAULT_CONNECT_TIMEOUT_SECONDS: Final[float] = 5.0
DEFAULT_READ_TIMEOUT_SECONDS: Final[float] = 30.0
"""ADR-0026 §3 requires both and names neither. Five seconds is generous for the loopback the
default allowlist permits; thirty is enough for a large export on a slow disk."""

ACCEPTED_MEDIA_TYPES: Final[tuple[str, ...]] = ("application/json",)
"""``Content-Type`` verified before parsing (ADR-0026 §3). A ``+json`` suffix is accepted too."""

EVIDENCE_EXPORT_PATH: Final[str] = "/api/v1/evidence/export"
"""FreeWeight's single-document endpoint: one ``benchmark.evidence_bundle`` envelope."""

EVIDENCE_COLLECTION_PATH: Final[str] = "/api/v1/evidence"
"""FreeWeight's collection endpoint. Not used for import — a bundle is one document
(ADR-0025 §2) — and named here so a reader knows which of the two this client speaks."""


class EvidenceSourceRefused(SuiteError):
    """LoadCoach declined to fetch, or to keep reading, a URL (spec §13, ADR-0026 §3).

    Raised *before* the body is interpreted. Distinct from
    :class:`~loadcoach.services.evidence.EvidenceImportFailed`, which means the bytes arrived and
    the bundle itself was unusable.
    """

    code: ClassVar[str] = "EVIDENCE_SOURCE_REFUSED"


class EvidenceSourceUnreachable(SuiteError):
    """The source was permitted but could not be reached, or answered with an error status.

    Carries ``EVIDENCE_IMPORT_FAILED`` because that is what it is from a caller's point of view:
    the import did not happen. The distinction that matters — *refused* versus *failed* — is
    preserved by the class, and by ``details["reason"]``.
    """

    code: ClassVar[str] = "EVIDENCE_IMPORT_FAILED"


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    """The fetch rules, as one value so a test can state them explicitly.

    Attributes:
        allowed_hosts: ``evidence.allowed_source_hosts``; loopback only by default.
        max_bytes: The streaming transfer cap (ADR-0026 §3's import limit).
        max_redirects: How many same-host redirects to follow.
        connect_timeout_seconds: Connect timeout.
        read_timeout_seconds: Read timeout.
    """

    allowed_hosts: tuple[str, ...]
    max_bytes: int = MAX_IMPORT_BYTES
    max_redirects: int = MAX_REDIRECTS
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class FetchedBundle:
    """One fetched document, unparsed.

    Attributes:
        document: The raw bytes. This client never parses them; that is the importer's job, and
            keeping the split makes "refused before parsing" mean something.
        url: The URL finally read, after any permitted redirect.
        content_type: The verified media type.
    """

    document: bytes
    url: str
    content_type: str


type Resolver = Callable[[str], Sequence[str]]
"""Maps a hostname to the IP addresses it resolves to. Injected so the link-local rule can be
tested without a DNS server that answers with one."""


def _default_resolver(host: str) -> Sequence[str]:
    """Resolve ``host`` to every address it names, or to nothing when it cannot be resolved."""
    try:
        return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]
    except OSError:
        return []


def policy_from_settings(settings: EvidenceSettings) -> FetchPolicy:
    """Build the fetch policy from ``[evidence]``.

    Args:
        settings: The ``[evidence]`` block.

    Returns:
        The :class:`FetchPolicy`.
    """
    return FetchPolicy(allowed_hosts=tuple(settings.allowed_source_hosts))


def resolve_credential(settings: EvidenceSettings) -> str | None:
    """Read the configured bearer token, from the environment or from a file.

    Configuration Standards §6's secret chain: a variable name, or a path, never the secret
    itself in a config file. Whitespace is stripped so a file written with a trailing newline
    works.

    Args:
        settings: The ``[evidence]`` block.

    Returns:
        The token, or ``None`` when none is configured or the source is empty or unreadable —
        an unreadable credential is "no credential", not a crash, because the endpoint may not
        need one.
    """
    import os
    from pathlib import Path

    if settings.freeweight_api_key_env:
        value = os.environ.get(settings.freeweight_api_key_env, "").strip()
        if value:
            return value
    if settings.freeweight_api_key_file:
        try:
            value = Path(settings.freeweight_api_key_file).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if value:
            return value
    return None


def _refuse(reason: str, message: str, **detail: object) -> EvidenceSourceRefused:
    """Build a refusal that names the rule it failed."""
    return EvidenceSourceRefused(message, details={"reason": reason, **detail})


def check_url(url: str, policy: FetchPolicy, *, resolve: Resolver = _default_resolver) -> httpx.URL:
    """Apply ADR-0026 §3's URL rules, or refuse.

    The order is deliberate: parse, then scheme, then host allowlist, then addresses. A
    ``file://`` URL fails on the scheme without a name ever being resolved, and a host outside
    the allowlist fails without a connection ever being opened.

    Args:
        url: The URL a request body supplied.
        policy: The fetch policy.
        resolve: Hostname resolution, injected.

    Returns:
        The parsed URL, permitted.

    Raises:
        EvidenceSourceRefused: The URL is unparsable, uses a scheme other than ``http``/``https``,
            names a host outside ``evidence.allowed_source_hosts``, or names — directly or
            through resolution — a link-local address.
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError) as exc:
        raise _refuse("malformed_url", f"{url!r} is not a URL LoadCoach can fetch.") from exc

    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise _refuse(
            "scheme_not_allowed",
            f"Evidence may be pulled over http or https only; {scheme or '(none)'!r} is refused "
            "(ADR-0026 §3).",
            scheme=scheme,
            url=url,
        )

    host = parsed.host
    if not host:
        raise _refuse("no_host", f"{url!r} names no host.", url=url)
    if host.lower() not in {allowed.lower() for allowed in policy.allowed_hosts}:
        raise _refuse(
            "host_not_allowed",
            f"{host!r} is not in evidence.allowed_source_hosts "
            f"({', '.join(policy.allowed_hosts) or 'empty'}). A remote FreeWeight must be listed "
            "explicitly (ADR-0026 §3).",
            host=host,
            allowed_hosts=list(policy.allowed_hosts),
        )

    for address in _addresses_of(host, resolve=resolve):
        if address.is_link_local:
            raise _refuse(
                "link_local_address",
                f"{host!r} names or resolves to the link-local address {address}, which is "
                "refused unconditionally — the cloud metadata range is the classic target "
                "(ADR-0026 §3).",
                host=host,
                address=str(address),
            )
    return parsed


def _addresses_of(
    host: str, *, resolve: Resolver
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return every IP a host names, whether it is a literal or a name to be resolved."""
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for candidate in resolve(host):
        try:
            addresses.append(ipaddress.ip_address(candidate.split("%")[0]))
        except ValueError:
            continue
    return addresses


def _check_content_type(response: httpx.Response, url: str) -> str:
    """Verify ``Content-Type`` before any of the body is interpreted."""
    raw = str(response.headers.get("content-type", ""))
    media_type = raw.split(";")[0].strip().lower()
    if media_type in ACCEPTED_MEDIA_TYPES or media_type.endswith("+json"):
        return media_type
    raise _refuse(
        "content_type_not_allowed",
        f"{url} answered with Content-Type {raw or '(none)'!r}; an evidence bundle is JSON "
        "(ADR-0026 §3, checked before parsing).",
        content_type=raw,
        url=url,
    )


class FreeWeightClient:
    """Pulls one ``benchmark.evidence_bundle`` from a FreeWeight instance.

    Constructed with a transport rather than a URL so that a test can drive it entirely in
    process, and so the one place that opens a socket is visible.
    """

    __slots__ = ("_client", "_policy", "_resolve")

    def __init__(
        self,
        policy: FetchPolicy,
        *,
        transport: httpx.BaseTransport | None = None,
        resolve: Resolver = _default_resolver,
    ) -> None:
        """Build a client bound to one fetch policy.

        Args:
            policy: The fetch rules.
            transport: An httpx transport, injected in tests.
            resolve: Hostname resolution, injected in tests.
        """
        self._policy = policy
        self._resolve = resolve
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(
                policy.read_timeout_seconds, connect=policy.connect_timeout_seconds
            ),
            follow_redirects=False,
        )

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> FreeWeightClient:
        """Support ``with FreeWeightClient(...) as client:``."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Always close the pool."""
        self.close()

    def fetch(
        self, url: str, *, since: datetime | None = None, credential: str | None = None
    ) -> FetchedBundle:
        """Fetch one bundle, obeying every rule in ADR-0026 §3.

        Args:
            url: The source URL. A bare origin gains :data:`EVIDENCE_EXPORT_PATH`, so
                ``{"url": "http://127.0.0.1:8765"}`` — api.md §7's own example — works.
            since: ADR-0022 §5's incremental filter. This is the ``generated_at`` of the previous
                bundle from **this** source, on FreeWeight's clock; LoadCoach never sends its own.
            credential: A bearer token for this host, or ``None``. Never invented here: the
                caller decides whether the configured credential belongs to this host.

        Returns:
            The unparsed :class:`FetchedBundle`.

        Raises:
            EvidenceSourceRefused: Any of the URL, redirect, content-type or size rules failed.
            EvidenceSourceUnreachable: The host could not be reached, or answered with a
                non-success status.
        """
        target = self._with_export_path(check_url(url, self._policy, resolve=self._resolve))
        if since is not None:
            from baseaicore.timeutil import to_rfc3339

            target = target.copy_set_param("since", to_rfc3339(since))

        headers = {"Accept": "application/json"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"

        origin_host = target.host
        redirects = 0
        while True:
            response = self._send(target, headers)
            if response.is_redirect:
                response.close()
                redirects += 1
                target = self._next_hop(response, target, origin_host, redirects)
                continue
            return self._read(response, target)

    def _send(self, target: httpx.URL, headers: dict[str, str]) -> httpx.Response:
        """Open a streaming request, translating transport failures."""
        request = self._client.build_request("GET", target, headers=headers)
        try:
            return self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise EvidenceSourceUnreachable(
                f"Could not reach {target}: {exc}",
                details={"reason": "transport_error", "url": str(target)},
            ) from exc

    def _next_hop(
        self, response: httpx.Response, target: httpx.URL, origin_host: str, redirects: int
    ) -> httpx.URL:
        """Decide where a redirect may go, or refuse."""
        if redirects > self._policy.max_redirects:
            raise _refuse(
                "too_many_redirects",
                f"{target} redirected more than {self._policy.max_redirects} times (ADR-0026 §3).",
                url=str(target),
                max_redirects=self._policy.max_redirects,
            )
        location = response.headers.get("location", "")
        nxt = target.join(location)
        if nxt.host != origin_host:
            raise _refuse(
                "cross_host_redirect",
                f"{target} redirected to {nxt.host!r}, a different host from {origin_host!r}; "
                "redirects are not followed across a host change, and no credential is ever "
                "forwarded across one (ADR-0026 §3).",
                url=str(target),
                redirect_host=nxt.host,
                origin_host=origin_host,
            )
        check_url(str(nxt), self._policy, resolve=self._resolve)
        return nxt

    def _read(self, response: httpx.Response, target: httpx.URL) -> FetchedBundle:
        """Verify the head, then stream the body under the size cap."""
        try:
            if response.status_code >= 400:  # noqa: PLR2004 — HTTP's own boundary
                raise EvidenceSourceUnreachable(
                    f"{target} answered {response.status_code}.",
                    details={
                        "reason": "http_status",
                        "status_code": response.status_code,
                        "url": str(target),
                    },
                )
            content_type = _check_content_type(response, str(target))
            declared = response.headers.get("content-length")
            if (
                declared is not None
                and declared.isdigit()
                and int(declared) > (self._policy.max_bytes)
            ):
                raise _refuse(
                    "too_large",
                    f"{target} declared {declared} bytes; the import limit is "
                    f"{self._policy.max_bytes} (ADR-0026 §3).",
                    declared_bytes=int(declared),
                    max_bytes=self._policy.max_bytes,
                )
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > self._policy.max_bytes:
                    raise _refuse(
                        "too_large",
                        f"{target} sent more than the {self._policy.max_bytes}-byte import "
                        "limit; the transfer was stopped rather than read to the end "
                        "(ADR-0026 §3).",
                        max_bytes=self._policy.max_bytes,
                        url=str(target),
                    )
                chunks.append(chunk)
        finally:
            response.close()
        return FetchedBundle(document=b"".join(chunks), url=str(target), content_type=content_type)

    @staticmethod
    def _with_export_path(url: httpx.URL) -> httpx.URL:
        """Point a bare origin at FreeWeight's export endpoint."""
        if url.path in ("", "/"):
            return url.copy_with(path=EVIDENCE_EXPORT_PATH)
        return url
