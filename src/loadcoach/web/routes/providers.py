"""loadcoach.web.routes.providers — ``GET/PUT/DELETE /providers`` and the Providers page.

[ADR-0117](../../docs/adr/0117-provider-registrations-are-edited-in-place-in-the-config-file.md):
the configuration file stays the source of truth and these handlers edit it in place. They parse,
call :mod:`loadcoach.services.providers`, re-register the running server, and render.

``providers.allow_remote`` is not here. It is the egress boundary and stays config-only, so a
registration declaring ``remote = true`` can be added from this page and is refused by routing
until a human edits the file to allow remote providers at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Body, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from loadcoach.config import load_settings
from loadcoach.domain.authorization import authorize
from loadcoach.infrastructure.providers.factory import build_registrations
from loadcoach.services.providers import (
    WRITABLE_FIELDS,
    config_digest,
    delete_registration,
    describe_registrations,
    save_registration,
)
from loadcoach.web.auth import CurrentPrincipal
from loadcoach.web.csrf import render_form_page

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["providers"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)

_BOOLEAN_FIELDS = frozenset({"remote"})
_NUMERIC_FIELDS = frozenset({"timeout_seconds"})


def _config_path(request: Request) -> Path:
    """The file this application was loaded from — what an edit writes."""
    return Path(request.app.state.config_path)


def _reregister(request: Request) -> None:
    """Re-read the file and point the running server at the registrations it now names.

    Only the provider handles are rebuilt. Every other value this process captured at startup
    keeps what it captured (ADR-0117 decision 5), and the page says so.
    """
    app = request.app
    settings = load_settings(config_path=app.state.config_path).settings
    app.state.settings.providers = settings.providers
    app.state.settings.provider = settings.provider
    registrations = build_registrations(app.state.settings)
    app.state.provider_registrations = registrations
    app.state.provider = registrations[0].provider
    runtime = app.state.queue_runtime
    if runtime is not None:
        runtime.replace_registrations(registrations)


def _document(request: Request) -> dict[str, Any]:
    """Every configured registration, the file they live in, and its digest."""
    path = _config_path(request)
    return {
        "registrations": [
            view.as_json() for view in describe_registrations(request.app.state.settings)
        ],
        "config_path": str(path),
        "config_digest": config_digest(path),
        "allow_remote": request.app.state.settings.providers.allow_remote,
        "writable_fields": list(WRITABLE_FIELDS),
    }


@router.get("/providers", summary="The configured provider registrations")
def get_providers(request: Request, principal: CurrentPrincipal) -> dict[str, Any]:
    """Every registration, with what an environment variable shadows, and the file they live in."""
    authorize(principal, "read")
    return _document(request)


@router.put("/providers/{name}", summary="Create or change one registration")
def put_provider(
    request: Request,
    name: str,
    principal: CurrentPrincipal,
    body: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Write ``[providers.<name>]`` into the configuration file; ``admin`` scope.

    ``400 VALIDATION_ERROR`` names a key this refuses to write or a value the registration model
    rejects; ``409 CONFLICT`` means the file changed since ``base_digest`` was read.
    """
    authorize(principal, "admin")
    values = {key: value for key, value in body.items() if key != "base_digest"}
    save_registration(_config_path(request), name, values, base_digest=body.get("base_digest"))
    _reregister(request)
    return _document(request)


@router.delete("/providers/{name}", summary="Remove one registration")
def delete_provider(request: Request, name: str, principal: CurrentPrincipal) -> dict[str, Any]:
    """Remove ``[providers.<name>]``; ``admin`` scope. The last registration cannot be removed."""
    authorize(principal, "admin")
    delete_registration(_config_path(request), name)
    _reregister(request)
    return _document(request)


@ui_router.get("/providers", summary="Providers page", response_class=HTMLResponse)
def providers_page(request: Request, principal: CurrentPrincipal) -> HTMLResponse:
    """Render every registration as an editable form, plus one empty form to add another."""
    authorize(principal, "read")
    return render_form_page(
        request,
        "providers/index.html",
        page="providers",
        document=_document(request),
        saved=request.query_params.get("saved") == "1",
    )


@ui_router.post("/providers", summary="Save or remove from the page")
async def providers_form(request: Request, principal: CurrentPrincipal) -> RedirectResponse:
    """The Providers form: one registration per submit, ``action`` says save or delete."""
    authorize(principal, "admin")
    # Parsed here rather than through ``request.form()``: the page posts
    # ``application/x-www-form-urlencoded`` only, and that needs no extra dependency.
    raw = parse_qs((await request.body()).decode("utf-8", "replace"))
    form = {key: values[-1] for key, values in raw.items()}
    name = form.get("name", "").strip()
    base_digest = form.get("base_digest")
    path = _config_path(request)
    if form.get("action") == "delete":
        delete_registration(path, name, base_digest=base_digest)
    else:
        values: dict[str, Any] = {}
        for field_name in WRITABLE_FIELDS:
            if field_name in _BOOLEAN_FIELDS:
                values[field_name] = field_name in form
                continue
            if field_name not in form:
                continue
            text = form[field_name].strip()
            values[field_name] = float(text) if field_name in _NUMERIC_FIELDS and text else text
        save_registration(path, name, values, base_digest=base_digest)
    _reregister(request)
    return RedirectResponse("/providers?saved=1", status_code=status.HTTP_303_SEE_OTHER)
