"""End-to-end: the server boots with zero configuration and serves a real request.

Uses ``provider.kind = "fake"`` throughout (never a real Ollama) so this suite passes with no GPU,
no Ollama and no network — spec §20 acceptance criterion 11.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from loadcoach.bootstrap import bootstrap


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    application = bootstrap()
    with TestClient(application.app, base_url="http://localhost") as test_client:
        yield test_client


def test_server_boots_with_zero_configuration_and_serves_health(client: TestClient) -> None:
    """Acceptance criterion 1: starts with no configuration file present at all."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    names = {component["name"] for component in body["components"]}
    assert names == {"database", "provider", "queue", "evidence", "reliability"}


def test_the_documented_health_component_lists_match_the_endpoint(client: TestClient) -> None:
    """F10 (M5C-10): spec §17, api.md §1 and ``/api/v1/health`` name one component list.

    M5-5 added ``reliability`` everywhere but left spec §17 promising a ``gpu_telemetry``
    component no phase ever built; this parses the mirrored documents so the three sources
    cannot drift apart silently again.
    """
    import re
    from pathlib import Path

    served = {item["name"] for item in client.get("/api/v1/health").json()["components"]}
    docs = Path(__file__).resolve().parents[2] / "docs" / "apps" / "loadcoach"

    spec = (docs / "spec.md").read_text(encoding="utf-8")
    bullet_start = spec.index("* Health components:")
    bullet = spec[bullet_start : spec.index("\n* ", bullet_start)]
    assert set(re.findall(r"`([a-z_]+)`", bullet)) == served, bullet

    api = (docs / "api.md").read_text(encoding="utf-8")
    row = next(line for line in api.splitlines() if "`GET /health`" in line)
    listed = row.split("Components:", 1)[1]
    assert set(re.findall(r"`([a-z_]+)`", listed)) == served, row


def test_health_reports_degraded_with_no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance criterion 1: reports degraded health with no provider reachable."""
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "ollama")
    monkeypatch.setenv("LOADCOACH_PROVIDER__BASE_URL", "http://127.0.0.1:1")
    application = bootstrap()
    with TestClient(application.app, base_url="http://localhost") as test_client:
        response = test_client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    provider_component = next(c for c in body["components"] if c["name"] == "provider")
    assert provider_component["status"] == "unavailable"


def test_health_response_has_request_id_header(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert "X-Request-ID" in response.headers


def test_version_endpoint_unauthenticated(client: TestClient) -> None:
    """ADR-0026 §5: version negotiation works before any credential is established."""
    response = client.get("/api/v1/version")
    assert response.status_code == 200
    body = response.json()
    assert body["application"]["name"] == "loadcoach"
    assert body["api"]["current"] == "v1"


def test_wrong_host_header_rejected_with_421(client: TestClient) -> None:
    """ADR-0026 §1: DNS-rebinding defence — an unrecognized Host header is refused."""
    response = client.get("/api/v1/health", headers={"Host": "evil.example.com"})
    assert response.status_code == 421
    body = response.json()
    assert body["error"]["code"] == "MISDIRECTED_REQUEST"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


def test_loopback_host_variants_all_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    application = bootstrap()
    with TestClient(application.app, base_url="http://localhost") as test_client:
        for host in ("localhost", "127.0.0.1"):
            response = test_client.get("/api/v1/health", headers={"Host": host})
            assert response.status_code == 200, host


def test_request_id_is_echoed_back(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "my-custom-id-123"})
    assert response.headers["X-Request-ID"] == "my-custom-id-123"


def test_unrecognized_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "has spaces! invalid"})
    assert response.headers["X-Request-ID"] != "has spaces! invalid"


def test_database_migrates_on_first_boot(client: TestClient) -> None:
    """Acceptance criterion 2: the database migrates through WeightsDB on startup."""
    response = client.get("/api/v1/health")
    database_component = next(c for c in response.json()["components"] if c["name"] == "database")
    assert database_component["status"] == "ok"
    assert "at head" in database_component["detail"]
