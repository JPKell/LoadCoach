"""loadcoach.web.routes.evidence — the evidence API and the Benchmarks page (api.md §7).

Three API endpoints and one page, because they are three views of one thing: an imported
``capability.evidence`` record.

* ``POST /api/v1/evidence/import`` — a bundle in the body, or ``{"url": …}`` to pull one. The
  URL form goes through the fetch allowlist (ADR-0026 §3), and the endpoint is ``admin``-scoped
  because imported evidence is untrusted input (spec §14).
* ``GET /api/v1/evidence`` — a **collection** envelope whose items are ``capability.evidence``
  SetSpec envelopes (ADR-0025 §2).
* ``GET /api/v1/evidence/sources`` — configured and observed sources with their last status.
* ``GET /evidence`` — the Benchmarks page: coverage per capability, then the records themselves
  with source, age, confidence and staleness.

No business logic here. Version negotiation, binding, staleness and the fetch rules are
:mod:`loadcoach.services.evidence`'s and
:mod:`loadcoach.infrastructure.freeweight_client`'s; these handlers parse a request, call one
service function, and render.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from baseaicore import ValidationError
from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import HTMLResponse

from loadcoach.__about__ import __version__
from loadcoach.domain.authorization import authorize
from loadcoach.domain.evidence_policy import MATCH_STATES
from loadcoach.infrastructure.freeweight_client import FreeWeightClient, policy_from_settings
from loadcoach.services.evidence import (
    DEFAULT_EVIDENCE_LIMIT,
    MAX_EVIDENCE_LIMIT,
    EvidenceQuery,
    capability_coverage,
    credential_for,
    evidence_overview,
    import_bundle,
    last_generated_at,
    list_sources,
    query_evidence,
)
from loadcoach.web.auth import CurrentPrincipal
from loadcoach.web.rendering import render

if TYPE_CHECKING:
    from loadcoach.config import Settings
    from loadcoach.services.database import Database

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["evidence"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)

_CapabilityQuery = Annotated[str | None, Query(description="Exact capability ID.")]
_ModelQuery = Annotated[str | None, Query(description="Model canonical ID.")]
_MatchStateQuery = Annotated[
    str | None, Query(alias="match_state", description="bound | unmatched | ambiguous_name_only.")
]
_MinConfidenceQuery = Annotated[
    float | None, Query(alias="min_confidence", ge=0.0, le=1.0, description="Confidence floor.")
]
_StaleQuery = Annotated[bool | None, Query(description="Stale records only, or fresh only.")]
_LimitQuery = Annotated[int, Query(ge=1, le=MAX_EVIDENCE_LIMIT, description="Page size.")]
_CursorQuery = Annotated[str | None, Query(description="The previous page's next_cursor.")]


@router.post("/evidence/import", summary="Import a FreeWeight evidence bundle")
def import_evidence(
    request: Request, principal: CurrentPrincipal, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """Import a bundle from the request body, or pull one from a URL (api.md §7).

    Args:
        request: The incoming request.
        body: Either a ``benchmark.evidence_bundle`` envelope, or ``{"url": "http://…"}``.

    Returns:
        Counts of imported / updated / unmatched / rejected, with every rejection's reason.

    Raises:
        ValidationError: The body is neither an envelope nor a ``url``.
        EvidenceSchemaVersionUnsupported: An unsupported schema major; both versions are named
            and no evidence is changed.
        EvidenceImportFailed: The bundle itself was unusable.
        EvidenceSourceRefused: A ``url`` failed the fetch allowlist (ADR-0026 §3).
    """
    authorize(principal, "admin")
    settings: Settings = request.app.state.settings
    database: Database = request.app.state.database
    now = datetime.now(UTC)

    url = body.get("url")
    if isinstance(url, str) and url.strip():
        with FreeWeightClient(policy_from_settings(settings.evidence)) as client:
            fetched = client.fetch(
                url.strip(),
                since=last_generated_at(database, url=url.strip()),
                credential=credential_for(settings.evidence, url.strip()),
            )
        outcome = import_bundle(
            database,
            fetched.document,
            now=now,
            accept_schema_majors=settings.evidence.accept_schema_majors,
            source_kind="freeweight_api",
            url=url.strip(),
            principal=principal,
        )
        return outcome.as_json()

    if "schema" not in body:
        raise ValidationError(
            "POST /evidence/import takes either a benchmark.evidence_bundle envelope or "
            '{"url": "http://…"} naming a FreeWeight to pull from.',
            details={"field": "body"},
        )
    outcome = import_bundle(
        database,
        body,
        now=now,
        accept_schema_majors=settings.evidence.accept_schema_majors,
        source_kind="file",
        principal=principal,
    )
    return outcome.as_json()


@router.get("/evidence", summary="Imported capability evidence")
def list_evidence(  # noqa: PLR0913 — every argument is a documented query parameter
    request: Request,
    principal: CurrentPrincipal,
    capability: _CapabilityQuery = None,
    model: _ModelQuery = None,
    match_state: _MatchStateQuery = None,
    min_confidence: _MinConfidenceQuery = None,
    stale: _StaleQuery = None,
    limit: _LimitQuery = DEFAULT_EVIDENCE_LIMIT,
    cursor: _CursorQuery = None,
) -> dict[str, Any]:
    """Return imported evidence as a collection of SetSpec envelopes (ADR-0025 §2).

    Args:
        request: The incoming request.
        capability: Exact capability ID.
        model: Model canonical ID.
        match_state: One of ``bound``, ``unmatched``, ``ambiguous_name_only``.
        min_confidence: Confidence floor.
        stale: Stale records only, or fresh only.
        limit: Page size.
        cursor: The previous page's ``next_cursor``.

    Returns:
        ``{"items": [...], "page": {...}, "summary": {...}}``. ``items`` are
        ``capability.evidence`` envelopes; ``summary`` is the store overview the page and the
        routing explanation share.

    Raises:
        ValidationError: ``match_state`` is not one of the three.
    """
    authorize(principal, "read")
    import json

    if match_state is not None and match_state not in MATCH_STATES:
        raise ValidationError(
            f"match_state must be one of {sorted(MATCH_STATES)}; got {match_state!r}.",
            details={"field": "match_state", "value": match_state},
        )
    database: Database = request.app.state.database
    settings: Settings = request.app.state.settings
    now = datetime.now(UTC)
    page = query_evidence(
        database,
        EvidenceQuery(
            capability=capability,
            model=model,
            match_state=match_state,
            min_confidence=min_confidence,
            stale=stale,
            limit=limit,
            cursor=cursor,
        ),
        now=now,
    )
    return {
        "items": [json.loads(row.as_envelope(generator_version=__version__)) for row in page.items],
        "page": {
            "limit": page.limit,
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
            "total": page.total,
        },
        "summary": _summary_json(
            evidence_overview(database, configured_url=settings.evidence.freeweight_url.strip())
        ),
    }


@router.get("/evidence/sources", summary="Evidence sources and their last import")
def list_evidence_sources(request: Request, principal: CurrentPrincipal) -> dict[str, Any]:
    """Return every configured and observed source with its last status (api.md §7)."""
    authorize(principal, "read")
    database: Database = request.app.state.database
    settings: Settings = request.app.state.settings
    configured = settings.evidence.freeweight_url.strip()
    overview = evidence_overview(database, configured_url=configured)
    return {
        "sources": [
            source.as_json() for source in list_sources(database, configured_url=configured)
        ],
        "configured_url": configured or None,
        "summary": _summary_json(overview),
    }


def _summary_json(overview: Any) -> dict[str, Any]:  # noqa: ANN401 — EvidenceOverview
    """Render the store overview for an API response."""
    from baseaicore.timeutil import to_rfc3339

    def when(value: datetime | None) -> str | None:
        return None if value is None else to_rfc3339(value)

    return {
        "status": overview.status,
        "note": overview.note,
        "configured": overview.configured,
        "total_records": overview.rows,
        "bound_records": overview.bound,
        "unmatched_records": overview.unmatched,
        "ambiguous_records": overview.ambiguous,
        "stale_records": overview.stale,
        "imported_at": when(overview.imported_at),
        "generated_at": when(overview.generated_at),
        "oldest_measured_at": when(overview.oldest_measured_at),
        "newest_measured_at": when(overview.newest_measured_at),
        "bundle_schema_version": overview.bundle_schema_version,
        "policy_version": overview.policy_version,
        "vocabulary_version": overview.vocabulary_version,
    }


@ui_router.get("/evidence", summary="Benchmarks (evidence) page", response_class=HTMLResponse)
def evidence_page(request: Request, principal: CurrentPrincipal) -> HTMLResponse:
    """Render the Benchmarks page: coverage per capability, then the records behind it."""
    authorize(principal, "read")
    database: Database = request.app.state.database
    settings: Settings = request.app.state.settings
    configured = settings.evidence.freeweight_url.strip()
    now = datetime.now(UTC)
    page = query_evidence(database, EvidenceQuery(limit=MAX_EVIDENCE_LIMIT), now=now)
    return HTMLResponse(
        render(
            "evidence/index.html",
            page="evidence",
            overview=evidence_overview(database, configured_url=configured),
            coverage=capability_coverage(database),
            records=page.items,
            sources=list_sources(database, configured_url=configured),
        )
    )
