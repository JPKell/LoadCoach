"""loadcoach.web.rendering — the one Jinja environment every page renders through.

Since Phase 3 this is MirrorWall's environment, not a local one: the shell, the component macros,
the design tokens and the shared filters all come from the package, and this module supplies only
what is LoadCoach's — the product name, the navigation, and the template directory holding this
application's own pages.

Built once and cached: templates are compiled and cached on the environment, so a per-request
environment recompiles the layout on every page view.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mirrorwall import create_template_environment

from loadcoach.__about__ import __version__

if TYPE_CHECKING:
    from jinja2 import Environment

__all__ = [
    "APP_TABS",
    "NAV_ITEMS",
    "TELEMETRY_STREAM_URL",
    "configure_shell",
    "render",
    "templates",
]

_TEMPLATES_DIR = Path(__file__).parent / "templates"

TELEMETRY_STREAM_URL = "/api/v1/system/telemetry/stream"

APP_TABS: tuple[tuple[str, str], ...] = (
    ("freeweight", "FreeWeight"),
    ("loadcoach", "LoadCoach"),
    ("ideapress", "IdeaPress"),
    ("promptcadence", "PromptCadence"),
)
"""The suite's four applications, in the console's order, for the top-bar tab strip (row WM2).
Rendered only when ``[console] url`` is set; a peer is reached through the console
(``<url>/apps/<name>``), never on its own loopback port."""

NAV_ITEMS: tuple[dict[str, str], ...] = (
    {"key": "dashboard", "href": "/", "label": "Dashboard"},
    {"key": "models", "href": "/models", "label": "Models"},
    {"key": "providers", "href": "/providers", "label": "Providers"},
    {"key": "task-profiles", "href": "/task-profiles", "label": "Task profiles"},
    {"key": "routing", "href": "/routing", "label": "Routing"},
    {"key": "jobs", "href": "/jobs", "label": "Jobs"},
    {"key": "queue", "href": "/queue", "label": "Queue"},
    {"key": "evidence", "href": "/evidence", "label": "Benchmarks"},
    {"key": "reliability", "href": "/reliability", "label": "Reliability"},
    {"key": "system", "href": "/system", "label": "System"},
    {"key": "settings", "href": "/settings", "label": "Settings"},
)


@lru_cache(maxsize=1)
def templates() -> Environment:
    """Return the process-wide Jinja environment, building it on first use.

    MirrorWall supplies autoescaping, ``StrictUndefined`` and every shared filter; this function
    adds only the shell's slot values. LoadCoach's own templates come first on the search path, so
    a page here can override a package template by name if it ever needs to.
    """
    return create_template_environment(
        app_template_dirs=(_TEMPLATES_DIR,),
        globals_={
            "product_name": "LoadCoach",
            "product_version": __version__,
            "nav_items": NAV_ITEMS,
            "theme_storage_key": "loadcoach-theme",
            # UI standards §3: the telemetry bar is on every page. The URL is the sampled stream
            # ``services.telemetry_stream`` publishes; the bar's fields start as em dashes and the
            # script fills only what was reported.
            "show_telemetry_bar": True,
            "telemetry_stream_url": TELEMETRY_STREAM_URL,
            # MirrorWall 0.3 opt-ins (row WM2, design brief §6): inline meters on the four
            # telemetry groups, the product name as a link home, and the tab strip — which
            # renders nothing until `configure_shell` hands it a console URL.
            "telemetry_field_meters": ["cpu", "ram", "gpu", "vram"],
            "product_href": "/",
            "console_url": "",
            "app_tabs": APP_TABS,
            "own_app": "loadcoach",
        },
    )


def configure_shell(*, console_url: str) -> None:
    """Hand the shell what only the running configuration knows.

    Args:
        console_url: ``[console] url`` — WeightRoomGym's base URL, or ``""`` for no tab strip.
    """
    templates().globals["console_url"] = console_url.rstrip("/")


def render(template_name: str, /, **context: Any) -> str:
    """Render ``template_name`` with ``context``.

    Args:
        template_name: Path relative to ``web/templates/``, e.g. ``"models/index.html"``.
        **context: Template variables.

    Returns:
        The rendered HTML.
    """
    return templates().get_template(template_name).render(**context)
