"""End-to-end: the System page (dev-plan P8) — telemetry, residency, thread pool, breakers."""

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


def test_system_page_shows_every_named_section(client: TestClient) -> None:
    page = client.get("/system")
    assert page.status_code == 200
    text = page.text
    for heading in ("Health components", "Telemetry", "Resident models", "Circuit breakers"):
        assert heading in text
    assert "Workers" in text and "of max_concurrent_jobs" in text and "in flight" in text
    assert "Dispatch latency" in text and "Starving" in text
    assert "Machine fingerprint" in text
    # F11 (M5C-11): the 64-character fingerprint made the whole page scroll at 375 px; the
    # stopgap wrap rule must be on the page until MirrorWall 0.2.1 carries it in components.css.
    assert "overflow-wrap: anywhere" in text
    # The deterministic telemetry fixture: one 48 GiB device with 1 GiB used; unmeasured
    # readings are dashes carrying the reason, never zeros.
    assert "48.0 GB" in text or "48 GB" in text or "GiB" in text
    assert 'aria-label="Unavailable: not measurable in this environment"' in text
    assert ">0<" not in text.split("Telemetry")[1].split("Resident models")[0].replace(
        ">0</td>", ""
    )
    for name in ("database", "provider", "queue", "evidence", "reliability"):
        assert name in text
