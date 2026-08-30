"""loadcoach.web.routes.reliability — ``GET /reliability`` (api.md §7) and the Reliability page.

Route handlers contain no business logic: each calls
:func:`~loadcoach.services.reliability.reliability_report` and renders. The API, the page and
``loadcoach reliability show`` read the same report, so the three cannot disagree.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from loadcoach.domain.authorization import authorize
from loadcoach.domain.reliability import (
    MINIMUM_MEAN_SAMPLES,
    MINIMUM_PERCENTILE_SAMPLES,
    MINIMUM_RATE_SAMPLES,
    PRODUCTION_MINIMUM_SAMPLES,
    REGRESSION_MINIMUM_DROP,
    REGRESSION_MINIMUM_SAMPLES,
    REGRESSION_Z_THRESHOLD,
    WINDOWS,
)
from loadcoach.services.reliability import reliability_report
from loadcoach.web.auth import CurrentPrincipal
from loadcoach.web.rendering import render

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["reliability"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)

_TaskQuery = Annotated[str | None, Query(description="Only this task profile.")]
_ModelQuery = Annotated[str | None, Query(description="Only this model (canonical ID).")]

_MINIMUMS: dict[str, Any] = {
    "factor_attempts": PRODUCTION_MINIMUM_SAMPLES,
    "rate_samples": MINIMUM_RATE_SAMPLES,
    "mean_samples": MINIMUM_MEAN_SAMPLES,
    "percentile_samples": MINIMUM_PERCENTILE_SAMPLES,
    "regression_samples": REGRESSION_MINIMUM_SAMPLES,
    "regression_drop": REGRESSION_MINIMUM_DROP,
    "regression_z": REGRESSION_Z_THRESHOLD,
}


@router.get("/reliability", summary="Production evidence per model and task profile")
def get_reliability(
    request: Request,
    principal: CurrentPrincipal,
    task: _TaskQuery = None,
    model: _ModelQuery = None,
) -> dict[str, Any]:
    """Every tracked pair's window statistics, factor, regression verdict and breaker state.

    Every statistic carries the sample count behind it and a reason when it is absent
    (ADR-0016); ``minimums`` states the bounds so a reader can see why a value is missing.
    """
    authorize(principal, "read")
    entries = reliability_report(
        request.app.state.database, task_profile_id=task, canonical_id=model
    )
    return {
        "reliability": [entry.as_json() for entry in entries],
        "regressions": [
            {
                "canonical_id": entry.canonical_id,
                "task_profile_id": entry.task_profile_id,
                "reason": entry.regression.reason,
            }
            for entry in entries
            if entry.regression.regressed
        ],
        "windows": [window.name for window in WINDOWS],
        "minimums": _MINIMUMS,
    }


@ui_router.get("/reliability", summary="Reliability page", response_class=HTMLResponse)
def reliability_page(
    request: Request,
    principal: CurrentPrincipal,
    task: _TaskQuery = None,
    model: _ModelQuery = None,
) -> HTMLResponse:
    """Render every pair: acceptance, validation pass rate, latency distribution and trend."""
    authorize(principal, "read")
    entries = reliability_report(
        request.app.state.database, task_profile_id=task, canonical_id=model
    )
    return HTMLResponse(
        render(
            "reliability/index.html",
            page="reliability",
            entries=entries,
            windows=[window.name for window in WINDOWS],
            minimums=_MINIMUMS,
            task=task,
            model=model,
        )
    )
