"""Tests for loadcoach.services.health: the shape shared by the CLI and the API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from loadcoach.services.health import HealthComponent, get_health_report


class _FakeProviderHealth:
    def __init__(self, status: object, detail: str) -> None:
        self.status = status
        self.detail = detail


class _FakeProvider:
    def __init__(self, status: object, detail: str = "") -> None:
        self._status = status
        self._detail = detail

    def health(self) -> _FakeProviderHealth:
        return _FakeProviderHealth(self._status, self._detail)


def test_report_shape_has_status_version_checked_at_components() -> None:
    from modelrack.provider import ProviderStatus

    report = get_health_report(
        database=None,
        provider=_FakeProvider(ProviderStatus.OK, "ok"),  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC),
    )
    assert report.checked_at == "2026-08-26T12:00:00.000Z"
    assert isinstance(report.components, tuple)
    names = {c.name for c in report.components}
    assert names == {"database", "provider", "queue", "evidence", "reliability"}


def test_overall_status_is_ok_when_all_components_ok(tmp_path: Path) -> None:
    from modelrack.provider import ProviderStatus

    from loadcoach.services.database import Database, ensure_ready

    database = Database.from_url(f"sqlite:///{tmp_path / 'x.sqlite3'}")
    try:
        ensure_ready(database, auto_migrate=True)
        report = get_health_report(
            database=database,
            provider=_FakeProvider(ProviderStatus.OK),  # type: ignore[arg-type]
        )
        assert report.status == "ok"
    finally:
        database.close()


def test_unreachable_provider_degrades_but_never_makes_overall_unavailable(
    tmp_path: Path,
) -> None:
    """spec §5: LoadCoach starts and serves with no provider, reporting degraded health."""
    from modelrack.provider import ProviderStatus

    from loadcoach.services.database import Database, ensure_ready

    database = Database.from_url(f"sqlite:///{tmp_path / 'x.sqlite3'}")
    try:
        ensure_ready(database, auto_migrate=True)
        report = get_health_report(
            database=database,
            provider=_FakeProvider(ProviderStatus.UNAVAILABLE, "down"),  # type: ignore[arg-type]
        )
        assert report.status == "degraded"
        provider_component = next(c for c in report.components if c.name == "provider")
        assert provider_component.status == "unavailable"
    finally:
        database.close()


def test_database_component_degraded_when_unmigrated(tmp_path: Path) -> None:
    """database is the one required component — an unmigrated database degrades overall status."""
    from modelrack.provider import ProviderStatus

    from loadcoach.services.database import Database

    # ensure_ready() deliberately not called: a fresh, unmigrated database is a legal state
    # (database standards §5.1's "no alembic_version table -> treat as new") but not "at head".
    database = Database.from_url(f"sqlite:///{tmp_path / 'fresh.sqlite3'}")
    try:
        report = get_health_report(
            database=database,
            provider=_FakeProvider(ProviderStatus.OK),  # type: ignore[arg-type]
        )
        database_component = next(c for c in report.components if c.name == "database")
        assert database_component.status == "degraded"
        assert report.status == "degraded"
    finally:
        database.close()


def test_health_component_model_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        HealthComponent(name="x", status="ok", detail="d", unexpected="y")  # type: ignore[call-arg]


def test_evidence_is_not_configured_and_that_is_healthy(tmp_path: Path) -> None:
    """spec §6: LoadCoach is designed to run without FreeWeight, so "none" is not "broken"."""
    from loadcoach.config import load_settings
    from loadcoach.services.database import Database, ensure_ready

    database = Database.from_url(f"sqlite:///{tmp_path / 'e.sqlite3'}")
    try:
        ensure_ready(database, auto_migrate=True)
        settings = load_settings(config_path=tmp_path / "missing.toml").settings
        report = get_health_report(database=database, settings=settings)
        component = next(c for c in report.components if c.name == "evidence")
        assert component.status == "not_configured"
        assert "no evidence source is configured" in component.detail.lower()
        assert report.status in ("ok", "degraded")
    finally:
        database.close()


def test_evidence_degrades_when_the_configured_source_is_unreachable(
    tmp_path: Path, golden_bundle: dict[str, object], wrap_bundle: Callable[..., str]
) -> None:
    from datetime import UTC, datetime

    from loadcoach.config import load_settings
    from loadcoach.services.database import Database, ensure_ready
    from loadcoach.services.evidence import import_bundle, mark_source_unreachable

    config = tmp_path / "config.toml"
    config.write_text('[evidence]\nfreeweight_url = "http://127.0.0.1:8765"\n')
    database = Database.from_url(f"sqlite:///{tmp_path / 'e2.sqlite3'}")
    try:
        ensure_ready(database, auto_migrate=True)
        now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
        import_bundle(
            database,
            wrap_bundle(golden_bundle),
            now=now,
            source_kind="freeweight_api",
            url="http://127.0.0.1:8765",
        )
        settings = load_settings(config_path=config).settings
        healthy = get_health_report(database=database, settings=settings, clock=lambda: now)
        assert next(c for c in healthy.components if c.name == "evidence").status == "ok"

        mark_source_unreachable(
            database, url="http://127.0.0.1:8765", reason="connection refused", now=now
        )
        degraded = get_health_report(database=database, settings=settings, clock=lambda: now)
        component = next(c for c in degraded.components if c.name == "evidence")
        assert component.status == "degraded"
        assert "could not be reached" in component.detail
        assert degraded.status == "degraded"
    finally:
        database.close()
