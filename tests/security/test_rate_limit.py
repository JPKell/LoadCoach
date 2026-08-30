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


def _seeded_settings(tmp_path: Path, name: str, **server: object) -> Settings:
    """Two read tokens in a fresh database; three requests at once; auth brake at two failures."""
    url = f"sqlite:///{tmp_path / name}"
    seed = Database.from_url(url)
    ensure_ready(seed, auto_migrate=True)
    with seed.write() as session:
        for token_name, raw in (("a", "token-a"), ("b", "token-b")):
            session.add(
                ApiToken(
                    name=token_name,
                    token_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                    scope="read",
                    created_at=NOW,
                )
            )
    seed.close()
    return Settings(
        server=ServerSettings(
            rate_limit_per_minute=60,
            rate_limit_burst=3,
            failed_auth_per_minute=2,
            **server,  # type: ignore[arg-type]  # test-local keyword passthrough
        ),
        storage=StorageSettings(database_url=url),
        provider=ProviderSettings(kind="fake"),
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Three requests at once, then one a second; two tokens; auth brake at two failures."""
    settings = _seeded_settings(tmp_path, "rl.sqlite3")
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


def test_forwarded_for_from_an_untrusted_peer_is_ignored(tmp_path: Path) -> None:
    """F4 (M5C-4), the default half: with no trusted_proxies the header is anyone's to send.

    This is also Fable's reproduction of the lockout: everyone behind one address shares one
    failure budget, so once it is spent a *correct* token gets 429 for the rest of the minute.
    A forged X-Forwarded-For must not buy a stranger a fresh bucket — or pin their failures on
    someone else's address.
    """
    settings = _seeded_settings(tmp_path, "xff.sqlite3")
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        for index in range(2):
            forged = {**_auth("wrong"), "X-Forwarded-For": f"198.51.100.{index}"}
            assert client.get("/api/v1/health", headers=forged).status_code == 401
        escaped = {**_auth("token-a"), "X-Forwarded-For": "198.51.100.9"}
        assert client.get("/api/v1/health", headers=escaped).status_code == 429


def test_behind_a_trusted_proxy_the_brake_keys_on_the_forwarded_client(tmp_path: Path) -> None:
    """F4 (M5C-4): with the proxy in trusted_proxies, clients get their own addresses back.

    ADR-0014 §7 makes a TLS reverse proxy the standard non-loopback deployment; before this
    setting existed, twenty bad bearers a minute from anyone braked every user behind it.
    """
    settings = _seeded_settings(tmp_path, "proxy.sqlite3", trusted_proxies=("10.0.0.0/8",))
    with TestClient(
        create_app(settings), base_url="http://localhost", client=("10.9.9.9", 40000)
    ) as client:
        guesser = "198.51.100.7"
        for _ in range(2):
            forged = {**_auth("wrong"), "X-Forwarded-For": guesser}
            assert client.get("/api/v1/health", headers=forged).status_code == 401
        # The guessing client is braked — a correct token from the same client included.
        braked = {**_auth("token-a"), "X-Forwarded-For": guesser}
        assert client.get("/api/v1/health", headers=braked).status_code == 429
        # Another client behind the same proxy is untouched.
        neighbour = {**_auth("token-a"), "X-Forwarded-For": "198.51.100.8"}
        assert client.get("/api/v1/health", headers=neighbour).status_code == 200
        # A chain: hops inside the trusted networks are hops; the last untrusted entry is the
        # client, and everything left of it stays client-supplied text nobody believes.
        for _ in range(2):
            # A fresh wrong bearer: "wrong" already spent its own per-credential request bucket.
            chained = {**_auth("wrong-2"), "X-Forwarded-For": "203.0.113.5, 10.0.0.2"}
            assert client.get("/api/v1/health", headers=chained).status_code == 401
        chained_braked = {**_auth("token-b"), "X-Forwarded-For": "203.0.113.5, 10.0.0.2"}
        assert client.get("/api/v1/health", headers=chained_braked).status_code == 429
        chained_other = {**_auth("token-b"), "X-Forwarded-For": "203.0.113.9, 10.0.0.2"}
        assert client.get("/api/v1/health", headers=chained_other).status_code == 200


def test_resolve_client_address_edges() -> None:
    """The pure function's corners: all-trusted chains, unparseable entries, absent headers."""
    import ipaddress

    from loadcoach.web.rate_limit import resolve_client_address

    trusted = (ipaddress.ip_network("10.0.0.0/8"),)
    header = {"x-forwarded-for": "10.0.0.3, 10.0.0.2"}
    # A chain trusted all the way down falls back to the peer — never to nothing.
    assert resolve_client_address("10.9.9.9", Headers(header), trusted) == "10.9.9.9"
    # An unparseable entry cannot be a trusted proxy, so it is the client.
    garbled = Headers({"x-forwarded-for": "not-an-ip, 10.0.0.2"})
    assert resolve_client_address("10.9.9.9", garbled, trusted) == "not-an-ip"
    # No header from a trusted peer: the peer.
    assert resolve_client_address("10.9.9.9", Headers({}), trusted) == "10.9.9.9"
    # An untrusted peer's header is ignored entirely.
    assert resolve_client_address("192.0.2.1", Headers(header), trusted) == "192.0.2.1"
    # No trust configured: the header never matters.
    assert resolve_client_address("10.9.9.9", Headers(header), ()) == "10.9.9.9"
    assert resolve_client_address(None, Headers(header), trusted) is None


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
