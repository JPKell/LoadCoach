"""loadcoach.web.routes.models — GET /models (API §2) and the Models page.

Identity, availability and declared capabilities since P2; the evidence summary, reliability and
residency api.md §2 names arrive with P8, read from the same services their own pages use.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from loadcoach.domain.authorization import authorize
from loadcoach.services.models import ModelOverview, registry_overview
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
