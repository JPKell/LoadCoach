"""The fetch allowlist and the degradation contract (ADR-0026 §3/§4, dev-plan P6).

Every refusal test points the client at a server that answers with a **malformed body**. If a
check ran after the bytes were parsed, these tests would fail with a parse error instead of the
refusal they assert — which is how "before any bytes are parsed" is verified rather than claimed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from loadcoach.config import EvidenceSettings
from loadcoach.infrastructure.db.models import CapabilityEvidence, EvidenceSource
from loadcoach.infrastructure.freeweight_client import (
    EVIDENCE_EXPORT_PATH,
    MAX_IMPORT_BYTES,
    EvidenceSourceRefused,
    EvidenceSourceUnreachable,
    FetchPolicy,
    FreeWeightClient,
    check_url,
    resolve_credential,
)
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.evidence import (
    import_bundle,
    last_generated_at,
    list_sources,
    refresh_from_freeweight,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

MALFORMED = b'{"this is not": '
"""Deliberately unparsable. A refusal that happens after parsing cannot return this cleanly."""

LOOPBACK = FetchPolicy(allowed_hosts=("127.0.0.1", "localhost", "::1"))


def _database(tmp_path: Path) -> Database:
    database = Database.from_url(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    return database


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _json_response(body: bytes, *, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status, content=body, headers={"content-type": "application/json", **headers}
    )


# --------------------------------------------------------------------------------------------
# The five refusals — each before any byte of the body is interpreted
# --------------------------------------------------------------------------------------------


def test_a_file_url_is_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return _json_response(MALFORMED)

    with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
        with pytest.raises(EvidenceSourceRefused) as caught:
            client.fetch("file:///etc/passwd")
    assert caught.value.code == "EVIDENCE_SOURCE_REFUSED"
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "scheme_not_allowed"


def test_a_host_outside_the_allowlist_is_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return _json_response(MALFORMED)

    with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
        with pytest.raises(EvidenceSourceRefused) as caught:
            client.fetch("http://benchmarks.example.com:8765/api/v1/evidence/export")
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "host_not_allowed"
    assert "127.0.0.1" in str(caught.value)


def test_a_literal_link_local_address_is_refused_even_when_allowlisted() -> None:
    """169.254.169.254 is the classic metadata target; allowlisting it is not enough."""
    policy = FetchPolicy(allowed_hosts=("169.254.169.254",))

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return _json_response(MALFORMED)

    with FreeWeightClient(policy, transport=_transport(handler)) as client:
        with pytest.raises(EvidenceSourceRefused) as caught:
            client.fetch("http://169.254.169.254/latest/meta-data/")
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "link_local_address"


def test_a_name_that_resolves_into_link_local_space_is_refused() -> None:
    """ADR-0026 §3: "and any address that resolves into them"."""
    policy = FetchPolicy(allowed_hosts=("metadata.internal",))

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return _json_response(MALFORMED)

    with FreeWeightClient(
        policy, transport=_transport(handler), resolve=lambda _host: ["169.254.169.254"]
    ) as client:
        with pytest.raises(EvidenceSourceRefused) as caught:
            client.fetch("http://metadata.internal/api/v1/evidence/export")
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "link_local_address"


def test_an_ipv6_link_local_literal_is_refused() -> None:
    policy = FetchPolicy(allowed_hosts=("fe80::1",))

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return _json_response(MALFORMED)

    with FreeWeightClient(policy, transport=_transport(handler)) as client:
        with pytest.raises(EvidenceSourceRefused) as caught:
            client.fetch("http://[fe80::1]:8765/api/v1/evidence/export")
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "link_local_address"


def test_a_cross_host_redirect_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "127.0.0.1":
            return httpx.Response(302, headers={"location": "http://evil.example.com/steal"})
        return _json_response(MALFORMED)  # pragma: no cover - never reached

    with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
        with pytest.raises(EvidenceSourceRefused) as caught:
            client.fetch("http://127.0.0.1:8765")
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "cross_host_redirect"
    assert caught.value.details["redirect_host"] == "evil.example.com"


def test_a_same_host_redirect_is_followed_up_to_the_cap() -> None:
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(request.url.path)
        if len(hops) <= 2:
            return httpx.Response(302, headers={"location": f"/hop{len(hops)}"})
        return _json_response(b'{"ok": true}')

    with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
        fetched = client.fetch("http://127.0.0.1:8765")
    assert fetched.document == b'{"ok": true}'
    assert hops[0] == EVIDENCE_EXPORT_PATH


def test_more_redirects_than_the_cap_are_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"{request.url.path}/again"})

    with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
        with pytest.raises(EvidenceSourceRefused) as caught:
            client.fetch("http://127.0.0.1:8765")
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "too_many_redirects"


def test_a_non_json_content_type_is_refused_before_parsing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=MALFORMED, headers={"content-type": "text/html"})

    with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
        with pytest.raises(EvidenceSourceRefused) as caught:
            client.fetch("http://127.0.0.1:8765")
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "content_type_not_allowed"


# --------------------------------------------------------------------------------------------
# The size cap is a streaming limit
# --------------------------------------------------------------------------------------------


def test_an_oversize_body_is_refused_and_the_transfer_is_stopped_early() -> None:
    """The endless-body test: a limit applied after the read would never return.

    The server streams for ever. A client that materializes the response before checking its
    size cannot pass this; one that counts as it goes stops after a bounded number of chunks.
    """
    pulled = 0

    def endless() -> Iterator[bytes]:
        nonlocal pulled
        while True:
            pulled += 1
            yield b"{" * 1024

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=endless(), headers={"content-type": "application/json"})

    policy = FetchPolicy(allowed_hosts=("127.0.0.1",), max_bytes=8 * 1024)
    with FreeWeightClient(policy, transport=_transport(handler)) as client:
        with pytest.raises(EvidenceSourceRefused) as caught:
            client.fetch("http://127.0.0.1:8765")
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "too_large"
    assert pulled <= 16, f"the transfer read {pulled} chunks before stopping"


def test_a_declared_content_length_over_the_cap_is_refused_before_the_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=MALFORMED,
            headers={
                "content-type": "application/json",
                "content-length": str(MAX_IMPORT_BYTES + 1),
            },
        )

    with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
        with pytest.raises(EvidenceSourceRefused) as caught:
            client.fetch("http://127.0.0.1:8765")
    assert caught.value.details is not None
    assert caught.value.details["reason"] == "too_large"
    assert caught.value.details["declared_bytes"] == MAX_IMPORT_BYTES + 1


# --------------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------------


def test_a_credential_from_the_environment_is_sent_to_its_own_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return _json_response(b'{"ok": true}')

    monkeypatch.setenv("FW_TOKEN", "  secret-token \n")
    settings = EvidenceSettings(
        freeweight_url="http://127.0.0.1:8765", freeweight_api_key_env="FW_TOKEN"
    )
    assert resolve_credential(settings) == "secret-token"
    with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
        client.fetch("http://127.0.0.1:8765", credential=resolve_credential(settings))
    assert seen == ["Bearer secret-token"]


def test_a_credential_from_a_file_is_read_and_stripped(tmp_path: Path) -> None:
    key_file = tmp_path / "token"
    key_file.write_text("from-a-file\n")
    settings = EvidenceSettings(freeweight_api_key_file=str(key_file))
    assert resolve_credential(settings) == "from-a-file"
    assert resolve_credential(EvidenceSettings(freeweight_api_key_file=str(tmp_path / "nope"))) is (
        None
    )


def test_a_credential_configured_for_one_source_is_never_sent_to_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §14, and the one rule ADR-0026 §4 exists to make possible."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return _json_response(b'{"ok": true}')

    monkeypatch.setenv("FW_TOKEN", "secret-token")
    settings = EvidenceSettings(
        freeweight_url="http://127.0.0.1:8765",
        freeweight_api_key_env="FW_TOKEN",
        allowed_source_hosts=("127.0.0.1", "localhost"),
    )
    from loadcoach.services.evidence import credential_for

    assert credential_for(settings, "http://127.0.0.1:8765") == "secret-token"
    assert credential_for(settings, "http://localhost:8765") is None
    assert credential_for(settings, "http://127.0.0.1:9999") is None

    with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
        client.fetch(
            "http://localhost:8765", credential=credential_for(settings, "http://localhost:8765")
        )
    assert seen == [None]


def test_no_credential_is_forwarded_across_a_redirect() -> None:
    """A cross-host redirect is refused outright, so nothing can be forwarded over one."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "127.0.0.1":
            return httpx.Response(302, headers={"location": "http://localhost:8765/x"})
        return _json_response(b'{"ok": true}')  # pragma: no cover - never reached

    with FreeWeightClient(
        FetchPolicy(allowed_hosts=("127.0.0.1", "localhost")), transport=_transport(handler)
    ) as client:
        with pytest.raises(EvidenceSourceRefused):
            client.fetch("http://127.0.0.1:8765", credential="secret-token")


# --------------------------------------------------------------------------------------------
# Pull, incremental pull, and degradation
# --------------------------------------------------------------------------------------------


def test_a_pull_imports_and_records_its_source(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    document = wrap_bundle(golden_bundle).encode()

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(document)

    database = _database(tmp_path)
    try:
        settings = EvidenceSettings(freeweight_url="http://127.0.0.1:8765")
        with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
            outcome = refresh_from_freeweight(database, settings, now=NOW, client=client)
        assert outcome is not None
        assert outcome.imported == 3
        (source,) = list_sources(database, configured_url=settings.freeweight_url)
        assert source.kind == "freeweight_api"
        assert source.last_status == "ok"
        assert source.rows == 3
        assert source.configured is True
    finally:
        database.close()


def test_the_next_pull_sends_the_producers_generated_at_back_as_since(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """ADR-0022 §5: a client never supplies its own clock."""
    produced = datetime(2026, 8, 21, 9, 30, tzinfo=UTC)
    document = wrap_bundle(golden_bundle, generated_at=produced).encode()
    queries: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params.get("since"))
        return _json_response(document)

    database = _database(tmp_path)
    try:
        settings = EvidenceSettings(freeweight_url="http://127.0.0.1:8765")
        with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
            refresh_from_freeweight(database, settings, now=NOW, client=client)
            assert last_generated_at(database, url=settings.freeweight_url) == produced
            refresh_from_freeweight(database, settings, now=NOW + timedelta(hours=1), client=client)
    finally:
        database.close()
    assert queries[0] is None, "a source never imported from gets a complete pull"
    assert queries[1] is not None
    assert queries[1].startswith("2026-08-21T09:30:00")


def test_an_unconfigured_source_is_not_an_unavailable_one(tmp_path: Path) -> None:
    """`freeweight_url = ""` means not configured, which is a different state entirely."""
    database = _database(tmp_path)
    try:
        assert refresh_from_freeweight(database, EvidenceSettings(), now=NOW) is None
        with database.read() as session:
            assert session.query(EvidenceSource).count() == 0
    finally:
        database.close()


def test_an_unreachable_freeweight_retains_the_last_import_and_marks_it_stale(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """The degradation contract: retained, badged, and routing goes on."""
    document = wrap_bundle(golden_bundle).encode()
    reachable = True

    def handler(_request: httpx.Request) -> httpx.Response:
        if not reachable:
            raise httpx.ConnectError("connection refused")
        return _json_response(document)

    database = _database(tmp_path)
    try:
        settings = EvidenceSettings(freeweight_url="http://127.0.0.1:8765")
        with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
            refresh_from_freeweight(database, settings, now=NOW, client=client)
            reachable = False
            assert (
                refresh_from_freeweight(
                    database, settings, now=NOW + timedelta(hours=1), client=client
                )
                is None
            )

        with database.read() as session:
            rows = session.query(CapabilityEvidence).all()
            assert len(rows) == 3, "the last import is retained"
            assert all(row.stale for row in rows)
            assert {row.stale_reason for row in rows} == {"source_unreachable"}
        (source,) = list_sources(database, configured_url=settings.freeweight_url)
        assert source.last_status == "unreachable"
        assert source.error_text is not None
        assert source.stale_rows == 3
    finally:
        database.close()


def test_a_refused_url_records_the_refusal_without_touching_evidence(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        import_bundle(
            database,
            wrap_bundle(golden_bundle),
            now=NOW,
            source_kind="freeweight_api",
            url="http://127.0.0.1:8765",
        )
        settings = EvidenceSettings(
            freeweight_url="http://127.0.0.1:8765", allowed_source_hosts=("::1",)
        )

        def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - unreached
            return _json_response(MALFORMED)

        with FreeWeightClient(
            FetchPolicy(allowed_hosts=("::1",)), transport=_transport(handler)
        ) as client:
            assert refresh_from_freeweight(database, settings, now=NOW, client=client) is None
        (source,) = list_sources(database, configured_url=settings.freeweight_url)
        assert source.last_status == "refused"
        assert source.rows == 3
        assert source.stale_rows == 0, "a refusal is not a staleness claim about the measurements"
    finally:
        database.close()


def test_an_http_error_status_is_unreachable_not_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=MALFORMED, headers={"content-type": "application/json"})

    with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
        with pytest.raises(EvidenceSourceUnreachable) as caught:
            client.fetch("http://127.0.0.1:8765")
    assert caught.value.code == "EVIDENCE_IMPORT_FAILED"
    assert caught.value.details is not None
    assert caught.value.details["status_code"] == 503


def test_check_url_refuses_a_malformed_url() -> None:
    with pytest.raises(EvidenceSourceRefused):
        check_url("http://", LOOPBACK)
    with pytest.raises(EvidenceSourceRefused):
        check_url("not a url at all", LOOPBACK)


def test_a_bare_origin_gains_the_export_path() -> None:
    """api.md §7's own example body is `{"url": "http://127.0.0.1:8765"}`."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _json_response(b"{}")

    with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
        client.fetch("http://127.0.0.1:8765")
        client.fetch("http://127.0.0.1:8765/api/v1/evidence/export?capability=reasoning")
    assert paths == [EVIDENCE_EXPORT_PATH, EVIDENCE_EXPORT_PATH]


def test_the_document_is_returned_unparsed(
    golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """The client never parses: that split is what makes 'refused before parsing' meaningful."""
    document = wrap_bundle(golden_bundle).encode()

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(document)

    with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
        fetched = client.fetch("http://127.0.0.1:8765")
    assert fetched.document == document
    assert json.loads(fetched.document)["schema"] == "benchmark.evidence_bundle"
    assert fetched.content_type == "application/json"


def test_a_failed_refresh_then_a_successful_one_leaves_one_source_row(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """Found by the I4 demonstration, not by a unit test.

    The scheduler's first refresh fired while FreeWeight was still starting, recording a
    placeholder source keyed by the URL. The first successful import then created a *second* row
    keyed by the producer's own ``source_id``, and every later lookup by URL raised
    ``MultipleResultsFound`` — routing included.
    """
    document = wrap_bundle(golden_bundle).encode()
    reachable = False

    def handler(_request: httpx.Request) -> httpx.Response:
        if not reachable:
            raise httpx.ConnectError("connection refused")
        return _json_response(document)

    database = _database(tmp_path)
    try:
        settings = EvidenceSettings(freeweight_url="http://127.0.0.1:8765")
        with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
            assert refresh_from_freeweight(database, settings, now=NOW, client=client) is None
            with database.read() as session:
                assert session.query(EvidenceSource).count() == 1
            reachable = True
            outcome = refresh_from_freeweight(
                database, settings, now=NOW + timedelta(minutes=1), client=client
            )
        assert outcome is not None
        with database.read() as session:
            rows = session.query(EvidenceSource).all()
        assert len(rows) == 1, "the placeholder is adopted, not duplicated"
        assert rows[0].source_key == "freeweight-bench-01"
        assert rows[0].last_status == "ok"
        assert rows[0].error_text is None
        (source,) = list_sources(database, configured_url=settings.freeweight_url)
        assert source.rows == 3
        assert last_generated_at(database, url=settings.freeweight_url) is not None
    finally:
        database.close()


def test_a_url_lookup_is_deterministic_when_two_rows_share_a_url(tmp_path: Path) -> None:
    """``evidence_sources.url`` is not unique and cannot be; the lookup must still be stable."""
    from loadcoach.services.evidence import source_for_url

    database = _database(tmp_path)
    try:
        with database.write() as session:
            session.add(
                EvidenceSource(
                    source_key="placeholder",
                    kind="freeweight_api",
                    url="http://127.0.0.1:8765",
                    record_count=0,
                    created_at=NOW,
                    last_status="unreachable",
                )
            )
            session.add(
                EvidenceSource(
                    source_key="freeweight-bench-01",
                    kind="freeweight_api",
                    url="http://127.0.0.1:8765",
                    record_count=3,
                    created_at=NOW,
                    last_import_at=NOW,
                    last_status="ok",
                )
            )
        with database.read() as session:
            chosen = source_for_url(session, "http://127.0.0.1:8765")
            assert chosen is not None
            assert chosen.source_key == "freeweight-bench-01", (
                "the row that has actually imported wins"
            )
            assert source_for_url(session, "http://elsewhere") is None
    finally:
        database.close()


def test_an_unreachable_source_with_nothing_imported_does_not_claim_retention(
    tmp_path: Path,
) -> None:
    from loadcoach.services.evidence import evidence_overview

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    database = _database(tmp_path)
    try:
        settings = EvidenceSettings(freeweight_url="http://127.0.0.1:8765")
        with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
            refresh_from_freeweight(database, settings, now=NOW, client=client)
        overview = evidence_overview(database, configured_url=settings.freeweight_url)
        assert overview.status == "unreachable"
        assert "nothing has been imported" in overview.note
        assert "0 of 0" not in overview.note
    finally:
        database.close()


def test_a_successful_import_clears_the_source_unreachable_badge(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """`source_unreachable` is a statement about the source, never about the measurement.

    Seen in the I4 demonstration: rows badged by a refresh that failed at startup were still
    badged after the source came back and imported successfully, which reads as "these
    measurements are suspect" when what was suspect was the connection.
    """
    document = wrap_bundle(golden_bundle).encode()
    reachable = True

    def handler(_request: httpx.Request) -> httpx.Response:
        if not reachable:
            raise httpx.ConnectError("connection refused")
        return _json_response(document)

    database = _database(tmp_path)
    try:
        settings = EvidenceSettings(freeweight_url="http://127.0.0.1:8765")
        with FreeWeightClient(LOOPBACK, transport=_transport(handler)) as client:
            refresh_from_freeweight(database, settings, now=NOW, client=client)
            reachable = False
            refresh_from_freeweight(database, settings, now=NOW + timedelta(hours=1), client=client)
            with database.read() as session:
                assert {row.stale_reason for row in session.query(CapabilityEvidence).all()} == {
                    "source_unreachable"
                }
            reachable = True
            refresh_from_freeweight(database, settings, now=NOW + timedelta(hours=2), client=client)
        with database.read() as session:
            reasons = {row.stale_reason for row in session.query(CapabilityEvidence).all()}
        assert "source_unreachable" not in reasons
    finally:
        database.close()
