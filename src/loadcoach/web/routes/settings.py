"""loadcoach.web.routes.settings — ``GET``/``PUT /settings`` (api.md §9) and the Settings page.

Runtime-changeable keys only. A security-relevant key is ``403 FORBIDDEN`` naming the key; an
unknown one is ``VALIDATION_ERROR`` naming it and listing what can be changed. Both come from
:mod:`loadcoach.services.settings`, so the API, the page and the CLI enforce one registry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Body, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from loadcoach.domain.authorization import authorize
from loadcoach.services.settings import (
    RUNTIME_SETTINGS,
    runtime_settings_document,
    write_runtime_settings,
)
from loadcoach.web.auth import CurrentPrincipal
from loadcoach.web.csrf import render_form_page

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["settings"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)


@router.get("/settings", summary="Runtime-changeable settings")
def get_settings(request: Request, principal: CurrentPrincipal) -> dict[str, Any]:
    """Every runtime-changeable key's effective value, its definition, and the config-only keys."""
    authorize(principal, "read")
    app = request.app
    return runtime_settings_document(app.state.database, settings=app.state.settings)


@router.put("/settings", summary="Change runtime settings")
def put_settings(
    request: Request, principal: CurrentPrincipal, body: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """Set one or more runtime-changeable keys; ``admin`` scope.

    ``403 FORBIDDEN`` names a security-relevant key; ``400 VALIDATION_ERROR`` names an unknown
    key or a value outside its bounds. Applied by the running scheduler within a second.
    """
    authorize(principal, "admin")
    app = request.app
    write_runtime_settings(
        app.state.database,
        body,
        settings=app.state.settings,
        now=datetime.now(UTC),
        principal=principal,
    )
    return runtime_settings_document(app.state.database, settings=app.state.settings)


@ui_router.get("/settings", summary="Settings page", response_class=HTMLResponse)
def settings_page(request: Request, principal: CurrentPrincipal) -> HTMLResponse:
    """Render every runtime-changeable setting as a form, and list what is config-only."""
    authorize(principal, "read")
    app = request.app
    document = runtime_settings_document(app.state.database, settings=app.state.settings)
    return render_form_page(
        request,
        "settings/index.html",
        page="settings",
        document=document,
        keys=list(RUNTIME_SETTINGS),
        saved=request.query_params.get("saved") == "1",
        config_path=None,
    )


@ui_router.post("/settings", summary="Save from the page")
async def settings_form(request: Request, principal: CurrentPrincipal) -> RedirectResponse:
    """The Settings form: booleans arrive as present/absent, numbers as text (CSRF-checked)."""
    authorize(principal, "admin")
    app = request.app
    # Parsed here rather than through ``request.form()``: the page posts
    # ``application/x-www-form-urlencoded`` only, and that needs no extra dependency.
    form = {
        key: values[-1]
        for key, values in parse_qs((await request.body()).decode("utf-8", "replace")).items()
    }
    changes: dict[str, Any] = {}
    for key, setting in RUNTIME_SETTINGS.items():
        raw = form.get(key)
        if setting.kind is bool:
            changes[key] = raw is not None and str(raw) in ("on", "true", "1")
            continue
        if raw is None or str(raw).strip() == "":
            continue
        text = str(raw).strip()
        changes[key] = int(text) if setting.kind is int else float(text)
    write_runtime_settings(
        app.state.database,
        changes,
        settings=app.state.settings,
        now=datetime.now(UTC),
        principal=principal,
    )
    return RedirectResponse("/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER)
