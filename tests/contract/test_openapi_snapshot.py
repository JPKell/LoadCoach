"""The committed OpenAPI snapshot matches the application (packaging standards §6)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from loadcoach.config import ProviderSettings, Settings
from loadcoach.web.app import create_app

SNAPSHOT = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"

pytestmark = pytest.mark.contract


def current_openapi() -> dict[str, Any]:
    """The schema this build serves, with the volatile version field pinned by the build."""
    document = create_app(Settings(provider=ProviderSettings(kind="fake"))).openapi()
    return cast("dict[str, Any]", json.loads(json.dumps(document, sort_keys=True)))


def test_the_committed_snapshot_matches_the_application() -> None:
    assert SNAPSHOT.is_file(), "docs/openapi.json is missing; regenerate it"
    committed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert committed == current_openapi(), (
        "docs/openapi.json drifted; regenerate with "
        "python -c 'from tests.contract.test_openapi_snapshot import write; write()'"
    )


def test_every_documented_endpoint_is_in_the_snapshot() -> None:
    paths = set(current_openapi()["paths"])
    for path in (
        "/api/v1/health",
        "/api/v1/version",
        "/api/v1/system/status",
        "/api/v1/models",
        "/api/v1/task-profiles",
        "/api/v1/route",
        "/api/v1/generate",
        "/api/v1/generate/stream",
        "/api/v1/jobs",
        "/api/v1/jobs/{job_id}",
        "/api/v1/jobs/{job_id}/stream",
        "/api/v1/jobs/{job_id}/cancel",
        "/api/v1/jobs/{job_id}/explanation",
        "/api/v1/jobs/{job_id}/feedback",
        "/api/v1/evidence/import",
        "/api/v1/evidence",
        "/api/v1/evidence/sources",
        "/api/v1/reliability",
        "/api/v1/queue",
        "/api/v1/queue/pause",
        "/api/v1/queue/resume",
        "/api/v1/queue/drain",
        "/api/v1/queue/stream",
        "/api/v1/settings",
        "/api/v1/system/telemetry/stream",
    ):
        assert path in paths, path


def write() -> None:
    """Regenerate the snapshot."""
    SNAPSHOT.write_text(json.dumps(current_openapi(), indent=2, sort_keys=True) + "\n")
