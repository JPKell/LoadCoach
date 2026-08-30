"""loadcoach.web.routes.routing — POST /route, decision retrieval, and the Routing page.

``POST /route`` runs routing §1's whole pipeline and returns the explanation **without**
executing (api.md §3): the cheapest way to understand the system, and the one to reach for when a
decision looks wrong.

``GET /routing-decisions/{decision_id}`` is not in api.md §3's table, which lists only the POST.
It is added here because acceptance criterion 2 requires every decision to be *retrievable* and
this phase has no ``jobs`` table for ``GET /jobs/{id}/explanation`` to hang off yet, and because
the Routing page needs a read path of its own. Additive within v1, as api.md's own preamble
requires.

Route handlers contain no business logic: each one calls a single service function and renders.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from loadcoach.domain.routing.narrative import narrate
from loadcoach.domain.routing.subject import RuntimeOverrides
from loadcoach.domain.task_profile import TaskProfileConstraints
from loadcoach.services.routing import (
    RouteRequest,
    read_decision,
    recent_decisions,
    route,
)
from loadcoach.web.rendering import render
from loadcoach.web.routing_support import (
    current_snapshot,
    provider_facts_for,
    routing_policy_for,
)

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["routing"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)


class RuntimeProfileOverrideBody(BaseModel):
    """The ``overrides.runtime_profile`` block (routing §10)."""

    model_config = ConfigDict(extra="forbid")

    context_size: int | None = Field(default=None, gt=0)
    kv_cache_precision: str | None = Field(default=None)
    gpu_layers: int | None = Field(default=None, ge=0)
    flash_attention: bool | None = Field(default=None)
    threads: int | None = Field(default=None, gt=0)
    batch_size: int | None = Field(default=None, gt=0)
    keep_alive: str | None = Field(default=None)


class OverridesBody(BaseModel):
    """Routing §10's overrides, as far as this phase implements them."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None)
    runtime_profile: RuntimeProfileOverrideBody | None = Field(default=None)
    disallow_fallback: bool = Field(default=False)
    require_evidence: bool = Field(default=False)


class RouteBody(BaseModel):
    """``POST /route``'s request body (api.md §3)."""

    model_config = ConfigDict(extra="forbid")

    task: str
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    constraints: TaskProfileConstraints | None = Field(default=None)
    overrides: OverridesBody | None = Field(default=None)


def _to_request(body: RouteBody) -> RouteRequest:
    from baseaicore import RuntimeProfile

    overrides = body.overrides or OverridesBody()
    profile_override = (
        None
        if overrides.runtime_profile is None
        else RuntimeProfile(**overrides.runtime_profile.model_dump(exclude_none=True))
    )
    return RouteRequest(
        task=body.task,
        estimated_input_tokens=body.estimated_input_tokens,
        max_output_tokens=body.max_output_tokens,
        constraints=body.constraints,
        overrides=RuntimeOverrides(
            model=overrides.model,
            runtime_profile=profile_override,
            disallow_fallback=overrides.disallow_fallback,
            require_evidence=overrides.require_evidence,
        ),
    )


@router.post("/route", summary="Route a task without executing it")
async def post_route(request: Request, body: RouteBody) -> dict[str, Any]:
    """Return the full routing explanation for ``body`` without spending a GPU second.

    Errors: ``TASK_PROFILE_NOT_FOUND`` when the task is unknown, ``NO_ELIGIBLE_MODEL`` (with every
    candidate and its rejection reason) when nothing survived the hard constraints.
    """
    app = request.app
    result = route(
        app.state.database,
        _to_request(body),
        provider=provider_facts_for(app.state.provider),
        policy=routing_policy_for(app.state.settings, database=app.state.database),
        snapshot=current_snapshot(app),
        now=datetime.now(UTC),
    )
    return result.explanation.payload


@router.get("/routing-decisions", summary="Recent routing decisions")
async def list_routing_decisions(request: Request) -> dict[str, object]:
    """Return the most recent decisions, newest first."""
    summaries = recent_decisions(request.app.state.database)
    return {
        "decisions": [
            {
                "decision_id": summary.decision_id,
                "task_profile": {
                    "id": summary.task_profile_id,
                    "version": summary.task_profile_version,
                },
                "requested_at": summary.requested_at.isoformat(),
                "duration_ms": summary.duration_ms,
                "selected": summary.selected_canonical_id,
                "final_score": summary.selected_score,
                "flags": list(summary.flags),
            }
            for summary in summaries
        ]
    }


@router.get("/routing-decisions/{decision_id}", summary="One stored routing explanation")
async def get_routing_decision(request: Request, decision_id: str) -> dict[str, Any]:
    """Return one decision's explanation exactly as it was persisted."""
    explanation = read_decision(request.app.state.database, decision_id)
    if explanation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such decision.")
    return explanation


@ui_router.get("/routing", summary="Routing page", response_class=HTMLResponse)
async def routing_page(request: Request) -> HTMLResponse:
    """Render the recent routing decisions."""
    summaries = recent_decisions(request.app.state.database)
    return HTMLResponse(render("routing/index.html", page="routing", decisions=summaries))


@ui_router.get("/routing/{decision_id}", summary="Explanation page", response_class=HTMLResponse)
async def routing_decision_page(request: Request, decision_id: str) -> HTMLResponse:
    """Render one decision's explanation as a readable table with every number behind it."""
    explanation = read_decision(request.app.state.database, decision_id)
    if explanation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such decision.")
    return HTMLResponse(
        render(
            "routing/detail.html",
            page="routing",
            explanation=explanation,
            narrative=narrate(explanation),
        )
    )
