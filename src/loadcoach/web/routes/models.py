"""loadcoach.web.routes.models — GET /models (API §2) and the plain HTML models page.

Evidence summary, reliability and residency (also named in API §2's `GET /models` row) arrive with
the phases that build them (P4, P6, P7); this phase's response carries identity, availability and
declared capabilities honestly, and nothing it does not yet have.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from loadcoach.services.models import RegistryEntry, list_registry
from loadcoach.web.rendering import render

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["models"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)


def _model_to_json(entry: RegistryEntry) -> dict[str, object]:
    return {
        "canonical_id": entry.canonical_id,
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
    }


@router.get("/models", summary="The model registry")
async def list_models(request: Request) -> dict[str, object]:
    """Return every known model, available or not, with declared capabilities.

    Unavailable models are included, with a reason — not deleted (dev-plan P2 test list).
    """
    entries = list_registry(request.app.state.database)
    return {"models": [_model_to_json(entry) for entry in entries]}


@ui_router.get("/models", summary="Models page", response_class=HTMLResponse)
async def models_page(request: Request) -> HTMLResponse:
    """Render the plain (pre-MirrorWall) models page."""
    entries = list_registry(request.app.state.database)
    return HTMLResponse(render("models/index.html", page="models", models=entries))
