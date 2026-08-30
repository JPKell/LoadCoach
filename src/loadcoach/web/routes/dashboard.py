"""loadcoach.web.routes.dashboard — the Dashboard page at ``/`` (dev-plan P8).

One service call, one render. The page links every headline figure to the page that owns it, so
nothing here is more than two interactions from its raw record (UI standards §5).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from loadcoach.services.dashboard import dashboard_report
from loadcoach.web.rendering import render

__all__ = ["ui_router"]

ui_router = APIRouter(tags=["ui"], include_in_schema=False)


@ui_router.get("/", summary="Dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request) -> HTMLResponse:
    """Current activity, queue health, recent decisions, model mix and degradations."""
    app = request.app
    report = dashboard_report(
        app.state.database,
        settings=app.state.settings,
        runtime=app.state.queue_runtime,
        provider=app.state.provider,
        now=datetime.now(UTC),
    )
    return HTMLResponse(render("dashboard/index.html", page="dashboard", report=report))
