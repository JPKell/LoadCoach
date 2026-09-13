"""loadcoach.web.routes.models — GET /models (API §2) and the Models page.

Identity, availability and declared capabilities since P2; the evidence summary, reliability and
residency api.md §2 names arrive with P8, read from the same services their own pages use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import parse_qs

from baseaicore import SuiteError
from fastapi import APIRouter, Body, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from loadcoach.domain.authorization import authorize
from loadcoach.domain.priority import JobClass
from loadcoach.domain.routing.subject import RuntimeOverrides
from loadcoach.infrastructure.providers.factory import disabled_registration_names
from loadcoach.services.models import (
    ModelOverview,
    discover_models,
    registry_overview,
    set_model_enabled,
)
from loadcoach.services.queue import JobSubmission, enqueue
from loadcoach.web.auth import CurrentPrincipal
from loadcoach.web.csrf import render_form_page

if TYPE_CHECKING:
    from loadcoach.services.database import Database

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["models"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)


def _model_to_json(overview: ModelOverview) -> dict[str, object]:
    """One registry entry as ``GET /models`` renders it (api.md §2).

    ``provider_name`` and ``is_remote`` carry the registration that served this model's most
    recent discovery and that registration's **declared** egress class, under the names the
    generate response's ``model`` block already uses (ADR-0099 rule 1). ``""`` and ``False`` mean
    *not recorded* — a row discovered before registrations had names — and are never replaced by a
    guess from the provider kind, which ADR-0055 rule 4 refuses.
    """
    entry = overview.entry
    return {
        "canonical_id": entry.canonical_id,
        "model_ref": entry.model_id,
        "provider_kind": entry.provider_kind,
        "provider_name": entry.provider_name,
        "is_remote": entry.is_remote,
        "provider_model_name": entry.provider_model_name,
        "identity_confidence": entry.identity_confidence,
        "family": entry.family,
        "quantization": entry.quantization,
        "max_context": entry.max_context,
        "size_bytes": entry.size_bytes,
        "parameter_count": entry.parameter_count,
        "available": entry.available,
        "unavailable_reason": entry.unavailable_reason,
        "enabled": entry.enabled,
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
    # ``render_form_page`` rather than ``render``: every row carries the enable/disable and warm
    # forms, and a form on this page needs the same CSRF token the settings page's does.
    return render_form_page(
        request,
        "models/index.html",
        page="models",
        models=overviews,
        scanned=request.query_params.get("scanned") == "1",
    )


class ModelNotFound(SuiteError):
    """No registry row matches ``model_ref`` — or more than one does (an ambiguous prefix)."""

    code = "MODEL_NOT_FOUND"


def _resolve(database: Database, model_ref: str) -> ModelOverview:
    """The one registry row ``model_ref`` names.

    ``model_ref`` is the registry ULID or an unambiguous prefix of it — never the canonical ID,
    which does not survive a path segment (ADR-0024).

    Raises:
        ModelNotFound: Nothing matches, or more than one row does.
    """
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
    return matches[0]


@router.post("/models/discover", summary="Re-discover models through the provider")
def discover(request: Request, principal: CurrentPrincipal) -> dict[str, Any]:
    """Run discovery now (api.md §2); ``admin``. Returns the added/updated/unavailable counts."""
    authorize(principal, "admin")
    app = request.app
    outcome = discover_models(
        app.state.database,
        getattr(app.state, "provider_registrations", None) or app.state.provider,
        now=datetime.now(UTC),
        disabled_provider_names=disabled_registration_names(app.state.settings),
        principal=principal,
    )
    return {
        "added": outcome.added,
        "updated": outcome.updated,
        "unavailable": outcome.unavailable,
        "total": outcome.total,
        "checked_at": outcome.checked_at.isoformat(),
        "unreachable": list(outcome.unreachable),
    }


@router.get("/models/{model_ref}", summary="One model in full")
def get_model(request: Request, principal: CurrentPrincipal, model_ref: str) -> dict[str, Any]:
    """Identity, descriptor, evidence per capability, reliability and breaker state (api.md §2).

    ``model_ref`` is the registry ULID or an unambiguous prefix of it — never the canonical ID,
    which does not survive a path segment (ADR-0024).
    """
    authorize(principal, "read")
    database = request.app.state.database
    overview = _resolve(database, model_ref)
    from sqlalchemy import select

    from loadcoach.infrastructure.db.models import CapabilityEvidence, Model
    from loadcoach.services.evidence import subject_canonical_id_of
    from loadcoach.services.reliability import reliability_report

    with database.read() as session:
        row = session.get(Model, overview.entry.model_id)
        descriptor = None if row is None else row.descriptor_json
        evidence = [
            {
                "subject_canonical_id": subject_canonical_id_of(item),
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


@router.post("/models/{model_ref}/enabled", summary="Permit or refuse a model")
def post_model_enabled(
    request: Request,
    principal: CurrentPrincipal,
    model_ref: str,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, object]:
    """Set the operator's ``enabled`` flag on one model (ADR-0118); ``admin`` scope.

    A disabled model stays in the registry with its evidence and its history; routing rejects it
    by name with ``model_disabled``, and asking for it explicitly is an error rather than a quiet
    substitution.
    """
    authorize(principal, "admin")
    database = request.app.state.database
    overview = _resolve(database, model_ref)
    set_model_enabled(
        database,
        model_id=overview.entry.model_id,
        enabled=bool(body.get("enabled", True)),
        principal=principal,
    )
    return _model_to_json(_resolve(database, overview.entry.model_id))


@router.post("/models/{model_ref}/warm", summary="Load a model by running a job on it")
def post_model_warm(
    request: Request, principal: CurrentPrincipal, model_ref: str
) -> dict[str, object]:
    """Make a model resident by submitting one small pinned job through the ordinary queue.

    Loading a model is something the executor does on the way to running work: admission,
    residency and eviction all live on that path, and a second way in would be a second admission
    policy to keep correct. So this enqueues a `general.chat` job pinned to the model
    (routing §10's ``overrides.model``) and lets that path do the loading it already does.
    """
    authorize(principal, "write")
    app = request.app
    overview = _resolve(app.state.database, model_ref)
    runtime = app.state.queue_runtime
    outcome = enqueue(
        app.state.database,
        JobSubmission(
            task="general.chat",
            prompt="Reply with the single word: ready.",
            overrides=RuntimeOverrides(model=overview.entry.canonical_id, disallow_fallback=True),
            job_class=JobClass("interactive"),
            idempotent=True,
            source="loadcoach.ui",
        ),
        now=datetime.now(UTC),
        queue_settings=app.state.settings.queue,
        execution_settings=app.state.settings.execution,
        sink=app.state.event_sink,
        wakeup=None if runtime is None else runtime.wakeup,
        principal=principal,
    )
    return {"job_id": outcome.job_id, "model_ref": overview.entry.model_id}


@ui_router.post("/models/{model_ref}/enabled", summary="Enable or disable from the page")
async def models_enabled_form(
    request: Request, principal: CurrentPrincipal, model_ref: str
) -> RedirectResponse:
    """The models page's enable/disable buttons (CSRF-checked)."""
    authorize(principal, "admin")
    raw = parse_qs((await request.body()).decode("utf-8", "replace"))
    database = request.app.state.database
    overview = _resolve(database, model_ref)
    set_model_enabled(
        database,
        model_id=overview.entry.model_id,
        enabled=raw.get("enabled", ["false"])[-1] == "true",
        principal=principal,
    )
    return RedirectResponse("/models", status_code=status.HTTP_303_SEE_OTHER)


@ui_router.post("/models/discover", summary="Scan for models from the page")
async def models_discover_form(request: Request, principal: CurrentPrincipal) -> RedirectResponse:
    """The models page's scan button: one discovery pass over every registration."""
    discover(request, principal)
    return RedirectResponse("/models?scanned=1", status_code=status.HTTP_303_SEE_OTHER)


@ui_router.post("/models/{model_ref}/warm", summary="Warm from the page")
async def models_warm_form(
    request: Request, principal: CurrentPrincipal, model_ref: str
) -> RedirectResponse:
    """The models page's warm button: submits the pinned job and shows it on the jobs page."""
    outcome = post_model_warm(request, principal, model_ref)
    return RedirectResponse(f"/jobs/{outcome['job_id']}", status_code=status.HTTP_303_SEE_OTHER)
