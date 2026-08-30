"""loadcoach.web.routes.models — GET /models (API §2) and the Models page.

Identity, availability and declared capabilities since P2; the evidence summary, reliability and
residency api.md §2 names arrive with P8, read from the same services their own pages use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from baseaicore import SuiteError
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from loadcoach.domain.authorization import authorize
from loadcoach.services.models import ModelOverview, discover_models, registry_overview
from loadcoach.web.auth import CurrentPrincipal
from loadcoach.web.rendering import render

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["models"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)


def _model_to_json(overview: ModelOverview) -> dict[str, object]:
    entry = overview.entry
    return {
        "canonical_id": entry.canonical_id,
        "model_ref": entry.model_id,
        "provider_kind": entry.provider_kind,
        "provider_model_name": entry.provider_model_name,
        "identity_confidence": entry.identity_confidence,
        "family": entry.family,
        "quantization": entry.quantization,
        "max_context": entry.max_context,
        "size_bytes": entry.size_bytes,
        "parameter_count": entry.parameter_count,
        "available": entry.available,
        "unavailable_reason": entry.unavailable_reason,
        "declared_capabilities": entry.declared_capabilities,
        "first_seen_at": entry.first_seen_at.isoformat(),
        "last_seen_at": entry.last_seen_at.isoformat(),
        **overview.as_json(),
    }


@router.get("/models", summary="The model registry")
async def list_models(request: Request, principal: CurrentPrincipal) -> dict[str, object]:
    """Return every known model, available or not, with declared capabilities, its evidence
    summary, reliability and residency (api.md §2).

    Unavailable models are included, with a reason — not deleted (dev-plan P2 test list).
    """
    authorize(principal, "read")
    overviews = registry_overview(request.app.state.database)
    return {"models": [_model_to_json(overview) for overview in overviews]}


@ui_router.get("/models", summary="Models page", response_class=HTMLResponse)
async def models_page(request: Request, principal: CurrentPrincipal) -> HTMLResponse:
    """Render every model with evidence coverage, reliability and residency."""
    authorize(principal, "read")
    overviews = registry_overview(request.app.state.database)
    return HTMLResponse(render("models/index.html", page="models", models=overviews))


class ModelNotFound(SuiteError):
    """No registry row matches ``model_ref`` — or more than one does (an ambiguous prefix)."""

    code = "MODEL_NOT_FOUND"


@router.post("/models/discover", summary="Re-discover models through the provider")
def discover(request: Request, principal: CurrentPrincipal) -> dict[str, Any]:
    """Run discovery now (api.md §2); ``admin``. Returns the added/updated/unavailable counts."""
    authorize(principal, "admin")
    app = request.app
    outcome = discover_models(
        app.state.database, app.state.provider, now=datetime.now(UTC), principal=principal
    )
    return {
        "added": outcome.added,
        "updated": outcome.updated,
        "unavailable": outcome.unavailable,
        "total": outcome.total,
        "checked_at": outcome.checked_at.isoformat(),
    }


@router.get("/models/{model_ref}", summary="One model in full")
def get_model(request: Request, principal: CurrentPrincipal, model_ref: str) -> dict[str, Any]:
    """Identity, descriptor, evidence per capability, reliability and breaker state (api.md §2).

    ``model_ref`` is the registry ULID or an unambiguous prefix of it — never the canonical ID,
    which does not survive a path segment (ADR-0024).
    """
    authorize(principal, "read")
    database = request.app.state.database
    matches = [
        overview
        for overview in registry_overview(database)
        if overview.entry.model_id.startswith(model_ref)
    ]
    if len(matches) != 1:
        raise ModelNotFound(
            f"No model matches {model_ref!r}."
            if not matches
            else f"{model_ref!r} is ambiguous: {len(matches)} models start with it.",
            details={"model_ref": model_ref, "matches": [m.entry.model_id for m in matches]},
        )
    overview = matches[0]
    from sqlalchemy import select

    from loadcoach.infrastructure.db.models import CapabilityEvidence, Model
    from loadcoach.services.reliability import reliability_report

    with database.read() as session:
        row = session.get(Model, overview.entry.model_id)
        descriptor = None if row is None else row.descriptor_json
        evidence = [
            {
                "capability_id": item.capability_id,
                "score": item.score,
                "confidence": item.confidence,
                "sample_count": item.sample_count,
                "source": "benchmark",
                "match_state": item.match_state,
                "runtime_profile_hash": item.runtime_profile_hash,
                "machine_fingerprint": item.machine_fingerprint,
                "measured_at": item.measured_at.isoformat(),
                "age_days": (datetime.now(UTC) - item.measured_at).days,
                "stale": item.stale,
                "stale_reason": item.stale_reason,
            }
            for item in session.execute(
                select(CapabilityEvidence)
                .where(CapabilityEvidence.model_id == overview.entry.model_id)
                .order_by(CapabilityEvidence.capability_id)
            ).scalars()
        ]
    reliability = [
        entry.as_json()
        for entry in reliability_report(database, canonical_id=overview.entry.canonical_id)
    ]
    return {
        **_model_to_json(overview),
        "descriptor": descriptor,
        "evidence": evidence,
        "reliability_by_task_profile": reliability,
        "circuit_breaker": {"state": overview.reliability["circuit_state"]},
    }
