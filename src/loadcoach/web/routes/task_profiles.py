"""loadcoach.web.routes.task_profiles — GET /task-profiles (API §2) and the plain HTML page."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from loadcoach.services.task_profiles import StoredTaskProfile, list_stored_task_profiles
from loadcoach.web.rendering import render

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["task-profiles"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)


def _profile_to_json(profile: StoredTaskProfile) -> dict[str, object]:
    return {
        "profile_id": profile.profile_id,
        "version": profile.version,
        "description": profile.description,
        "weights": profile.weights,
        "constraints": profile.constraints,
        "execution": profile.execution,
        "validation": profile.validation,
        "enabled": profile.enabled,
        "updated_at": profile.updated_at.isoformat(),
    }


@router.get("/task-profiles", summary="Task profile definitions")
async def list_task_profiles(request: Request) -> dict[str, object]:
    """Return every task profile: version, weights, constraints, execution and validation policy."""
    profiles = list_stored_task_profiles(request.app.state.database)
    return {"task_profiles": [_profile_to_json(profile) for profile in profiles]}


@ui_router.get("/task-profiles", summary="Task profiles page", response_class=HTMLResponse)
async def task_profiles_page(request: Request) -> HTMLResponse:
    """Render the plain (pre-MirrorWall) task profiles page."""
    profiles = list_stored_task_profiles(request.app.state.database)
    return HTMLResponse(
        render("task_profiles/index.html", page="task-profiles", task_profiles=profiles)
    )
