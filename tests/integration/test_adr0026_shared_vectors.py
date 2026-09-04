"""The ADR-0026 §3 vector set, shared byte-for-byte with ToolYard's ``http_fetch``.

The same checks defend against the same class of request in both places — this application pulling
an evidence bundle from a URL a request body supplied, and ToolYard's agent tool fetching a URL a
model supplied — so they are proven against **one** fixture rather than two suites that agree until
somebody edits one. ``tests/fixtures/fetch/adr0026_vectors.json`` is byte-identical to
``py/ToolYard/tests/fixtures/fetch/adr0026_vectors.json``, and :data:`VECTORS_SHA256` is asserted in
both repositories, so a drift fails a test here and there rather than being discovered later by
someone comparing behaviour in production.

**Nothing in this file changes LoadCoach's behaviour.** It drives the existing `FreeWeightClient`
through the shared cases and asserts the reason strings it already produces. Vectors that belong to
one implementation and not the other are deliberately absent from the shared file: this
application's credential handling (ToolYard has no credential surface at all), its `since`
parameter, and its bare-origin export path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx
import pytest

from loadcoach.infrastructure.freeweight_client import (
    EvidenceSourceRefused,
    EvidenceSourceUnreachable,
    FetchPolicy,
    FreeWeightClient,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

VECTORS_PATH: Final[Path] = (
    Path(__file__).parent.parent / "fixtures" / "fetch" / "adr0026_vectors.json"
)

VECTORS_SHA256: Final[str] = "ae7d6689ded17443ff6a944d567d343b1981acfb3e17d4ae87ed43bad0e91fcc"
"""The digest of the shared vector file, asserted in **both** repositories.

The file is authored in ToolYard, copied here byte-for-byte and proven with ``cmp``. Update this
constant only when that copy is redone, and move both digests in the same change.
"""

_DOCUMENT: Final[Mapping[str, Any]] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
VECTORS: Final[Sequence[Mapping[str, Any]]] = _DOCUMENT["vectors"]

DEFAULT_CAP: Final[int] = FetchPolicy(allowed_hosts=()).max_bytes
"""This application's configured cap, which the vectors express sizes relative to rather than
naming — it is 128 MiB here and 8 MiB in ToolYard."""


def _resolver(mapping: Mapping[str, Sequence[str]]) -> Callable[[str], Sequence[str]]:
    """Build the injected resolver a vector describes; an unnamed host resolves to nothing."""

    def resolve(host: str) -> Sequence[str]:
        return mapping.get(host, ())

    return resolve


def _transport(
    vector: Mapping[str, Any], *, cap: int, seen: list[httpx.Request]
) -> httpx.MockTransport:
    """Script the vector's responses in order, resolving every size against ``cap``."""
    scripted = list(vector["responses"])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        step = scripted[len(seen) - 1] if len(seen) <= len(scripted) else scripted[-1]
        if step.get("raise") == "connect_error":
            raise httpx.ConnectError("connection refused", request=request)
        headers = dict(step.get("headers", {}))
        if "body_bytes_over_cap" in step:
            body = b"a" * (cap + int(step["body_bytes_over_cap"]))
        else:
            body = str(step.get("body", "")).encode("utf-8")
        if "declared_length_over_cap" in step:
            headers["content-length"] = str(cap + int(step["declared_length_over_cap"]))
        if step.get("streamed"):
            # An iterator makes httpx send the body chunked, with no Content-Length — the only
            # shape in which the during-streaming check is the one that fires.
            size = int(step.get("chunk_bytes", 1024))
            content: Any = iter([body[at : at + size] for at in range(0, len(body), size)])
            return httpx.Response(int(step["status"]), content=content, headers=headers)
        return httpx.Response(int(step["status"]), content=body, headers=headers)

    return httpx.MockTransport(handler)


def test_the_shared_vector_file_has_not_drifted() -> None:
    """The fixture is ToolYard's, copied here. A change on one side alone is a divergence."""
    digest = hashlib.sha256(VECTORS_PATH.read_bytes()).hexdigest()
    assert digest == VECTORS_SHA256, (
        "tests/fixtures/fetch/adr0026_vectors.json differs from the copy this digest was taken "
        "from. The file is authored in py/ToolYard; re-copy it, prove it with `cmp`, and move "
        "VECTORS_SHA256 in both repositories together."
    )


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: str(vector["name"]))
def test_the_evidence_fetch_answers_every_adr0026_vector_the_same_way(
    vector: Mapping[str, Any],
) -> None:
    """One fixture, two implementations, the same machine-readable reason from each."""
    seen: list[httpx.Request] = []
    policy_kwargs: dict[str, Any] = {"allowed_hosts": tuple(vector["allowed_hosts"])}
    if vector["max_bytes"] is not None:
        policy_kwargs["max_bytes"] = int(vector["max_bytes"])
    if vector["max_redirects"] is not None:
        policy_kwargs["max_redirects"] = int(vector["max_redirects"])
    policy = FetchPolicy(**policy_kwargs)
    cap = policy.max_bytes
    expected = vector["expect"]

    with FreeWeightClient(
        policy,
        transport=_transport(vector, cap=cap, seen=seen),
        resolve=_resolver(vector["resolves"]),
    ) as client:
        if expected["outcome"] == "ok":
            bundle = client.fetch(vector["url"])
            assert bundle.content_type == expected["content_type"]
        else:
            failure = (
                EvidenceSourceRefused
                if expected["outcome"] == "refused"
                else EvidenceSourceUnreachable
            )
            with pytest.raises(failure) as caught:
                client.fetch(vector["url"])
            assert caught.value.details is not None
            assert caught.value.details["reason"] == expected["reason"]

    assert len(seen) == vector["requests_expected"], (
        f"{vector['name']}: made {len(seen)} requests, expected {vector['requests_expected']} — "
        "a check that must happen before a socket is opened shows here as zero."
    )
