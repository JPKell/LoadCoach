"""loadcoach.web.routes.access — the UI's token cookie (api.md §11, ADR-0014).

A browser cannot add ``Authorization`` to a page navigation, so on a tokened bind the same bearer
token is carried by the ``loadcoach_token`` cookie. It is set once from the 401 page by pasting the
token (CSRF-checked), and cleared by the same page. No account, no password, no session table: the
cookie *is* the bearer token, and revoking the token revokes the cookie with it.
"""

from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import APIRouter, Request, status
from fastapi.responses import RedirectResponse

from loadcoach.web.auth import TOKEN_COOKIE_NAME

__all__ = ["ui_router"]

ui_router = APIRouter(tags=["ui"], include_in_schema=False)


@ui_router.post("/token-cookie", summary="Carry a bearer token in the browser")
async def set_token_cookie(request: Request) -> RedirectResponse:
    """Store the pasted token as an ``HttpOnly``, ``Secure``, ``SameSite=Strict`` cookie."""
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    token = form.get("token", [""])[0].strip()
    target = form.get("next", ["/"])[0] or "/"
    if not target.startswith("/") or target.startswith("//"):
        target = "/"
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    if token:
        response.set_cookie(
            TOKEN_COOKIE_NAME, token, path="/", secure=True, httponly=True, samesite="strict"
        )
    return response


@ui_router.post("/token-cookie/clear", summary="Forget the browser's token")
async def clear_token_cookie(request: Request) -> RedirectResponse:
    """Delete the cookie; the next page load asks for a token again on a tokened bind."""
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(TOKEN_COOKIE_NAME, path="/")
    return response
