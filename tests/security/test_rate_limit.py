"""Per-token rate limits with ``Retry-After`` at the boundary (spec §14; ADR-0014 §6)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from tests.integration.test_generate import NOW

from loadcoach.config import ProviderSettings, ServerSettings, Settings, StorageSettings
from loadcoach.infrastructure.db.models import ApiToken
from loadcoach.services.database import Database, ensure_ready
from loadcoach.web.app import create_app
from loadcoach.web.rate_limit import TokenBucket, credential_key


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Three requests at once, then one a second; two tokens; auth brake at two failures."""
    url = f"sqlite:///{tmp_path / 'rl.sqlite3'}"
    seed = Database.from_url(url)
    ensure_ready(seed, auto_migrate=True)
    with seed.write() as session:
        for name, raw in (("a", "token-a"), ("b", "token-b")):
            session.add(
                ApiToken(
                    name=name,
                    token_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                    scope="read",
                    created_at=NOW,
                )
            )
    seed.close()
    settings = Settings(
        server=ServerSettings(
            rate_limit_per_minute=60, rate_limit_burst=3, failed_auth_per_minute=2
        ),
        storage=StorageSettings(database_url=url),
        provider=ProviderSettings(kind="fake"),
    )
    with TestClient(create_app(settings), base_url="http://localhost") as test_client:
        yield test_client


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def test_a_caller_at_the_limit_gets_429_with_retry_after_not_a_dropped_request(
    client: TestClient,
) -> None:
    for _ in range(3):
        assert client.get("/api/v1/health", headers=_auth("token-a")).status_code == 200
    limited = client.get("/api/v1/health", headers=_auth("token-a"))
    assert limited.status_code == 429
    body = limited.json()["error"]
    assert body["code"] == "RATE_LIMITED"
    assert int(limited.headers["Retry-After"]) >= 1
    assert body["details"]["retry_after_seconds"] == int(limited.headers["Retry-After"])
    assert limited.headers["X-Request-ID"]


def test_the_limit_is_per_token_not_per_connection(client: TestClient) -> None:
    for _ in range(3):
        client.get("/api/v1/health", headers=_auth("token-a"))
    assert client.get("/api/v1/health", headers=_auth("token-a")).status_code == 429
    # A different token has its own budget; the same token on a "new connection" does not.
    assert client.get("/api/v1/health", headers=_auth("token-b")).status_code == 200
    with TestClient(client.app, base_url="http://localhost") as second_connection:
        assert second_connection.get("/api/v1/health", headers=_auth("token-a")).status_code == 429


def test_version_is_exempt_and_pages_are_not_limited(client: TestClient) -> None:
    for _ in range(6):
        assert client.get("/api/v1/version").status_code == 200
    for _ in range(6):
        assert client.get("/", headers={"Cookie": "loadcoach_token=token-a"}).status_code == 200


def test_failed_authentication_is_braked_per_address(client: TestClient) -> None:
    for _ in range(2):
        assert client.get("/api/v1/health", headers=_auth("wrong")).status_code == 401
    braked = client.get("/api/v1/health", headers=_auth("wrong"))
    assert (
        braked.status_code == 429 and "failed authentications" in braked.json()["error"]["message"]
    )
    # The brake is on the address, whatever it presents now — a right token included.
    assert client.get("/api/v1/health", headers=_auth("token-b")).status_code == 429


def test_the_bucket_arithmetic_and_the_credential_key() -> None:
    bucket = TokenBucket(capacity=2.0, per_second=1.0, tokens=2.0, updated_at=0.0)
    assert bucket.take(0.0) == 0.0 and bucket.take(0.0) == 0.0
    assert bucket.take(0.0) == pytest.approx(1.0)  # one second until one token
    assert bucket.take(1.0) == 0.0  # refilled
    assert bucket.take(1.0) == pytest.approx(1.0)
    assert bucket.take(100.0) == 0.0 and bucket.tokens == pytest.approx(1.0)  # capped at capacity
    assert bucket.refill(100.0) == pytest.approx(1.0) and bucket.wait_seconds() == 0.0
    key, authenticated = credential_key(Headers({"authorization": "Bearer abc"}), "10.0.0.1")
    assert authenticated and key.startswith("token:") and "abc" not in key
    cookie_key, _ = credential_key(Headers({"cookie": "loadcoach_token=abc"}), "10.0.0.1")
    assert cookie_key == key  # the same credential, however carried
    assert credential_key(Headers({}), "10.0.0.1") == ("address:10.0.0.1", False)
    assert credential_key(Headers({"authorization": "Basic x"}), None) == ("address:unknown", False)


def test_zero_disables_the_request_limit(tmp_path: Path) -> None:
    settings = Settings(
        server=ServerSettings(rate_limit_per_minute=0, rate_limit_burst=1),
        storage=StorageSettings(database_url=f"sqlite:///{tmp_path / 'off.sqlite3'}"),
        provider=ProviderSettings(kind="fake"),
    )
    seed = Database.from_url(settings.storage.database_url or "")
    ensure_ready(seed, auto_migrate=True)
    seed.close()
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        for _ in range(20):
            assert client.get("/api/v1/health").status_code == 200
