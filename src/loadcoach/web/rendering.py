"""loadcoach.web.rendering — the Jinja environment the plain, pre-MirrorWall pages render through.

Not in the Phase 2 file list verbatim, but required by it: the Work item asks for "first UI pages
(plain, pre-MirrorWall)", and rendering them needs an environment somewhere — this mirrors
FreeWeight's own module of the same name and purpose, deliberately kept unstyled: MirrorWall's
design tokens land at Phase 4's extraction, not here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

__all__ = ["render", "templates"]

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _format_bytes(value: int | None) -> str:
    """Render a byte count at human scale; ``None`` becomes an em dash, never ``0``."""
    if value is None:
        return "—"
    if value < 1024:
        return f"{value} B"
    scaled = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        scaled /= 1024
        if scaled < 1024 or unit == "TiB":
            return f"{scaled:.1f} {unit}"
    raise AssertionError("unreachable: the TiB branch always returns")  # pragma: no cover


@lru_cache(maxsize=1)
def templates() -> Environment:
    """Return the process-wide Jinja environment, building it on first use.

    Autoescaping is on for HTML by default — a model name or a description reaches a template from
    outside this process (the provider, or an operator's own task profile file), and neither is
    trusted markup.
    """
    environment = Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        autoescape=select_autoescape(),
        auto_reload=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.filters["bytes"] = _format_bytes
    return environment


def render(template_name: str, /, **context: Any) -> str:
    """Render ``template_name`` with ``context``.

    Args:
        template_name: Path relative to ``web/templates/``, e.g. ``"models/index.html"``.
        **context: Template variables.

    Returns:
        The rendered HTML.
    """
    return templates().get_template(template_name).render(**context)
