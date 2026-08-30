"""The evidence API surface, the Benchmarks page and the CLI (api.md §7, spec §7.2)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi.testclient import TestClient
from modelrack.testing import FakeProvider
from typer.testing import CliRunner

from loadcoach.cli.main import app as cli_app
from loadcoach.config import load_settings
from loadcoach.infrastructure.db.models import ApiToken
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.evidence import import_bundle
from loadcoach.services.models import discover_models
from loadcoach.web.app import create_app
from loadcoach.web.auth import token_sha256

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    url = f"sqlite:///{tmp_path / 'api.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    settings = load_settings().settings
    database = Database.from_url(url)
    ensure_ready(database, auto_migrate=True)
    database.close()
    with TestClient(create_app(settings), base_url="http://localhost") as test_client:
        yield test_client


def _config(tmp_path: Path, body: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[storage]\ndatabase_url = "sqlite:///{tmp_path / "cli.sqlite3"}"\n{body}')
    return path


def _database(tmp_path: Path, name: str = "cli.sqlite3") -> Database:
    database = Database.from_url(f"sqlite:///{tmp_path / name}")
    ensure_ready(database, auto_migrate=True)
    return database


# --------------------------------------------------------------------------------------------
# POST /evidence/import
# --------------------------------------------------------------------------------------------


def test_import_accepts_a_bundle_body_and_reports_per_record_counts(
    client: TestClient, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    response = client.post("/api/v1/evidence/import", json=json.loads(wrap_bundle(golden_bundle)))
    assert response.status_code == 200
    body = response.json()
    assert body["imported"] == 3
    assert body["unmatched"] == 3
    assert body["rejected"] == []
    assert body["source_id"] == "freeweight-bench-01"


def test_import_rejects_an_unsupported_major_with_both_versions_and_422(
    client: TestClient, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    response = client.post(
        "/api/v1/evidence/import", json=json.loads(wrap_bundle(golden_bundle, major=2))
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "SCHEMA_VERSION_UNSUPPORTED"
    assert "2.0" in error["message"] + json.dumps(error["details"])
    assert error["details"]["accepted_majors"] == [1]


def test_import_refuses_a_url_outside_the_allowlist_with_403(client: TestClient) -> None:
    response = client.post(
        "/api/v1/evidence/import", json={"url": "http://benchmarks.example.com/export"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EVIDENCE_SOURCE_REFUSED"


def test_import_refuses_a_body_that_is_neither_a_bundle_nor_a_url(client: TestClient) -> None:
    response = client.post("/api/v1/evidence/import", json={"nonsense": True})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_import_is_admin_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden_bundle: dict[str, Any],
    wrap_bundle: Callable[..., str],
) -> None:
    """spec §14. Once any token exists, an unscoped or wrongly-scoped call is refused."""
    url = f"sqlite:///{tmp_path / 's.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    settings = load_settings().settings
    seed = Database.from_url(url)
    ensure_ready(seed, auto_migrate=True)
    seed.close()
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        database = Database.from_url(url)
        with database.write() as session:
            for name, raw, scope in (
                ("reader", "read-token", "read"),
                ("writer", "write-token", "write"),
                ("root", "admin-token", "admin"),
            ):
                session.add(
                    ApiToken(
                        name=name,
                        token_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                        scope=scope,
                        created_at=NOW,
                    )
                )
        database.close()
        document = json.loads(wrap_bundle(golden_bundle))

        assert client.post("/api/v1/evidence/import", json=document).status_code == 401
        forbidden = client.post(
            "/api/v1/evidence/import",
            json=document,
            headers={"Authorization": "Bearer read-token"},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "FORBIDDEN"
        assert (
            client.post(
                "/api/v1/evidence/import",
                json=document,
                headers={"Authorization": "Bearer write-token"},
            ).status_code
            == 403
        )
        allowed = client.post(
            "/api/v1/evidence/import",
            json=document,
            headers={"Authorization": "Bearer admin-token"},
        )
        assert allowed.status_code == 200
        assert token_sha256("admin-token") != "admin-token"


def test_an_oversize_import_is_rejected(client: TestClient) -> None:
    from loadcoach.services.evidence import MAX_PARSE_BYTES

    response = client.post(
        "/api/v1/evidence/import",
        content=b"{" * (MAX_PARSE_BYTES + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code in (400, 413, 422)


# --------------------------------------------------------------------------------------------
# GET /evidence and GET /evidence/sources
# --------------------------------------------------------------------------------------------


def test_evidence_items_are_capability_evidence_setspec_envelopes(
    client: TestClient, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """ADR-0025 §2: a collection envelope whose items are SetSpec envelopes."""
    from setspec import load_envelope
    from setspec.capability.v1 import CapabilityEvidenceIn

    client.post("/api/v1/evidence/import", json=json.loads(wrap_bundle(golden_bundle)))
    body = client.get("/api/v1/evidence").json()
    assert set(body) >= {"items", "page", "summary"}
    assert body["page"]["limit"] == 50
    assert body["page"]["total"] == 3
    assert body["page"]["has_more"] is False
    for item in body["items"]:
        envelope = load_envelope(item, expect="capability.evidence")
        assert envelope.generator.name == "loadcoach"
        CapabilityEvidenceIn.model_validate(envelope.payload)


def test_evidence_filters_and_pages(
    client: TestClient, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    client.post("/api/v1/evidence/import", json=json.loads(wrap_bundle(golden_bundle)))
    filtered = client.get("/api/v1/evidence", params={"capability": "reasoning"}).json()
    assert filtered["page"]["total"] == 1

    first = client.get("/api/v1/evidence", params={"limit": 2}).json()
    assert len(first["items"]) == 2
    assert first["page"]["has_more"] is True
    second = client.get(
        "/api/v1/evidence", params={"limit": 2, "cursor": first["page"]["next_cursor"]}
    ).json()
    assert len(second["items"]) == 1
    assert second["page"]["next_cursor"] is None

    bad = client.get("/api/v1/evidence", params={"match_state": "nonsense"})
    assert bad.status_code == 400


def test_sources_reports_not_configured_before_anything_is_imported(client: TestClient) -> None:
    body = client.get("/api/v1/evidence/sources").json()
    assert body["sources"] == []
    assert body["configured_url"] is None
    assert body["summary"]["status"] == "not_configured"
    assert "no evidence source is configured" in body["summary"]["note"].lower()


def test_sources_reports_the_last_import(
    client: TestClient, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    client.post("/api/v1/evidence/import", json=json.loads(wrap_bundle(golden_bundle)))
    body = client.get("/api/v1/evidence/sources").json()
    (source,) = body["sources"]
    assert source["source_id"] == "freeweight-bench-01"
    assert source["kind"] == "file"
    assert source["rows"] == 3
    assert body["summary"]["status"] == "ok"


# --------------------------------------------------------------------------------------------
# The Benchmarks page
# --------------------------------------------------------------------------------------------


def test_the_benchmarks_page_renders_empty_and_populated(
    client: TestClient, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    empty = client.get("/evidence")
    assert empty.status_code == 200
    assert "No capability has measured evidence" in empty.text
    assert "no evidence source is configured" in empty.text.lower()

    client.post("/api/v1/evidence/import", json=json.loads(wrap_bundle(golden_bundle)))
    populated = client.get("/evidence")
    assert populated.status_code == 200
    assert "coding.python" in populated.text
    assert "unmatched" in populated.text
    assert "freeweight-bench-01" in populated.text
    assert "kappa_w 0.74" in populated.text
    assert "18 held-out samples" in populated.text


def test_the_benchmarks_page_is_in_the_navigation(client: TestClient) -> None:
    assert 'href="/evidence"' in client.get("/models").text


def test_the_page_escapes_untrusted_evidence_content(
    client: TestClient, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """Imported evidence is untrusted input, and it reaches a template (spec §14)."""
    import copy

    bundle = copy.deepcopy(golden_bundle)
    bundle["source_id"] = '<script>alert("xss")</script>'
    client.post("/api/v1/evidence/import", json=json.loads(wrap_bundle(bundle)))
    page = client.get("/evidence")
    assert '<script>alert("xss")</script>' not in page.text
    assert "&lt;script&gt;" in page.text


# --------------------------------------------------------------------------------------------
# The CLI
# --------------------------------------------------------------------------------------------


def test_cli_import_show_and_sources_on_a_fresh_install(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """No `serve` has ever run: importing on a fresh install is exactly the common case."""
    runner = CliRunner()
    config = _config(tmp_path)
    assert runner.invoke(cli_app, ["db", "upgrade", "--config", str(config)]).exit_code == 0

    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(wrap_bundle(golden_bundle))
    imported = runner.invoke(
        cli_app, ["evidence", "import", "--file", str(bundle_file), "--config", str(config)]
    )
    assert imported.exit_code == 0, imported.output
    assert "imported    3" in imported.output
    assert "unmatched   3" in imported.output

    shown = runner.invoke(cli_app, ["evidence", "show", "--config", str(config), "--json"])
    assert shown.exit_code == 0
    payload = json.loads(shown.output)
    assert payload["total"] == 3
    assert {entry["capability_id"] for entry in payload["coverage"]} == {
        "coding.python",
        "user.noir_tech_voice",
        "reasoning",
    }

    sources = runner.invoke(cli_app, ["evidence", "sources", "--config", str(config)])
    assert sources.exit_code == 0
    assert "freeweight-bench-01" in sources.output
    assert "(not configured)" in sources.output


def test_cli_import_refuses_an_unsupported_major_with_exit_2(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    runner.invoke(cli_app, ["db", "upgrade", "--config", str(config)])
    bundle_file = tmp_path / "future.json"
    bundle_file.write_text(wrap_bundle(golden_bundle, major=2))
    result = runner.invoke(
        cli_app, ["evidence", "import", "--file", str(bundle_file), "--config", str(config)]
    )
    assert result.exit_code == 2
    assert "SCHEMA_VERSION_UNSUPPORTED" in result.output


def test_cli_import_refuses_a_file_url_with_exit_4(tmp_path: Path) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    runner.invoke(cli_app, ["db", "upgrade", "--config", str(config)])
    result = runner.invoke(
        cli_app, ["evidence", "import", "--url", "file:///etc/passwd", "--config", str(config)]
    )
    assert result.exit_code == 4
    assert "EVIDENCE_SOURCE_REFUSED" in result.output


def test_cli_import_needs_exactly_one_of_file_or_url(tmp_path: Path) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    runner.invoke(cli_app, ["db", "upgrade", "--config", str(config)])
    assert runner.invoke(cli_app, ["evidence", "import", "--config", str(config)]).exit_code == 2
    both = runner.invoke(
        cli_app,
        ["evidence", "import", "--file", "x", "--url", "http://127.0.0.1", "--config", str(config)],
    )
    assert both.exit_code == 2


def test_cli_refresh_says_not_configured_rather_than_failing(tmp_path: Path) -> None:
    """`freeweight_url = ""` is a state, not a fault — and the exit code distinguishes them."""
    runner = CliRunner()
    config = _config(tmp_path)
    runner.invoke(cli_app, ["db", "upgrade", "--config", str(config)])
    result = runner.invoke(cli_app, ["evidence", "refresh", "--config", str(config)])
    assert result.exit_code == 3
    assert "No evidence source is configured" in result.output


def test_cli_refresh_reports_an_unreachable_source_with_exit_4(tmp_path: Path) -> None:
    runner = CliRunner()
    config = _config(tmp_path, '[evidence]\nfreeweight_url = "http://127.0.0.1:1/export"\n')
    runner.invoke(cli_app, ["db", "upgrade", "--config", str(config)])
    result = runner.invoke(cli_app, ["evidence", "refresh", "--config", str(config)])
    assert result.exit_code == 4
    assert "unreachable" in result.output or "could not be reached" in result.output


def test_cli_show_reports_the_binding_state_after_discovery(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    runner = CliRunner()
    config = _config(tmp_path)
    runner.invoke(cli_app, ["db", "upgrade", "--config", str(config)])
    database = _database(tmp_path)
    try:
        import_bundle(database, wrap_bundle(golden_bundle), now=NOW)
        discover_models(database, FakeProvider(), now=NOW)
    finally:
        database.close()
    shown = runner.invoke(
        cli_app, ["evidence", "show", "--match-state", "unmatched", "--config", str(config)]
    )
    assert shown.exit_code == 0
    assert "unmatched" in shown.output

    bad = runner.invoke(
        cli_app, ["evidence", "show", "--match-state", "nope", "--config", str(config)]
    )
    assert bad.exit_code == 2


def test_the_http_import_and_the_cli_import_agree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    golden_bundle: dict[str, Any],
    wrap_bundle: Callable[..., str],
) -> None:
    """One service function behind both surfaces, asserted rather than assumed."""
    runner = CliRunner()
    config = _config(tmp_path)
    runner.invoke(cli_app, ["db", "upgrade", "--config", str(config)])
    bundle_file = tmp_path / "bundle.json"
    bundle_file.write_text(wrap_bundle(golden_bundle))
    cli_result = runner.invoke(
        cli_app,
        ["evidence", "import", "--file", str(bundle_file), "--config", str(config), "--json"],
    )
    assert cli_result.exit_code == 0
    cli_payload = json.loads(cli_result.output)

    url = f"sqlite:///{tmp_path / 'http.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    settings = load_settings().settings
    seed = Database.from_url(url)
    ensure_ready(seed, auto_migrate=True)
    seed.close()
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        http_payload = client.post(
            "/api/v1/evidence/import", json=json.loads(wrap_bundle(golden_bundle))
        ).json()

    for key in ("imported", "updated", "unmatched", "bound", "ambiguous_name_only", "total"):
        assert cli_payload[key] == http_payload[key], key


def test_a_url_import_over_http_goes_through_the_client(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    golden_bundle: dict[str, Any],
    wrap_bundle: Callable[..., str],
) -> None:
    document = wrap_bundle(golden_bundle).encode()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=document, headers={"content-type": "application/json"})

    from loadcoach.infrastructure.freeweight_client import FreeWeightClient

    original = FreeWeightClient.__init__

    def patched(self: FreeWeightClient, policy: Any, **kwargs: Any) -> None:
        original(self, policy, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("loadcoach.web.routes.evidence.FreeWeightClient.__init__", patched)
    response = client.post("/api/v1/evidence/import", json={"url": "http://127.0.0.1:8765"})
    assert response.status_code == 200
    assert response.json()["imported"] == 3
    sources = client.get("/api/v1/evidence/sources").json()["sources"]
    assert sources[0]["kind"] == "freeweight_api"
    assert sources[0]["url"] == "http://127.0.0.1:8765"
