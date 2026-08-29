"""Tests for loadcoach.services.health: the shape shared by the CLI and the API."""

from __future__ import annotations

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
    assert names == {"database", "provider"}


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
