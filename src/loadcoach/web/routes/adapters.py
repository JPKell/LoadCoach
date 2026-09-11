"""loadcoach.web.routes.adapters — ``GET /adapters`` (api.md §2).

The adapter registry the CLI's ``adapters list`` reads, over HTTP: the operator's console reaches
LoadCoach only through its API (ADR-0126 binds LoadCoach loopback), and the models view shows an
adapter only under a base it can serve, with neither its residency nor the routes that named it.
Read-only: a scan drafts manifests a person must review (ADR-0061 rule 4) and stays the CLI's.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from loadcoach.services.adapters import adapter_report
from loadcoach.web.auth import CurrentPrincipal

__all__ = ["router"]

router = APIRouter(tags=["adapters"])


@router.get("/adapters", summary="The adapter registry")
def get_adapters(request: Request, principal: CurrentPrincipal) -> dict[str, Any]:
    """Every adapter the directory describes, the providers holding it, residency and routes."""
    state = request.app.state
    return adapter_report(
        state.database,
        state.settings,
        tuple(getattr(state, "provider_registrations", None) or ()),
        principal=principal,
    )
