"""UI/UX Standards §13 — every item a test can hold the rendered pages to, held (dev-plan P8).

Each test names the §13 bullet it covers. Items that need a browser (layout at 1280×720 and 375
px, JavaScript disabled in a real client, the network panel offline, visual focus rings) are
listed at the bottom with what a person should do; items that need charts do not apply, because
LoadCoach draws none. Colour contrast is MirrorWall's: LoadCoach's templates use no colour of
their own, which the hard-coded-colour test proves, so the shared token test stands for every
page here.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from loadcoach.bootstrap import bootstrap
from loadcoach.infrastructure.db.models import Model
from loadcoach.services.database import Database
from loadcoach.web.rendering import NAV_ITEMS

HOSTILE = '<script>alert("xss")</script>'
TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "loadcoach" / "web" / "templates"


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """One populated server for the whole module: a hostile model, a finished job, feedback.

    Module-scoped, so it is created *before* the function-scoped XDG isolation in
    ``tests/conftest.py`` runs — which is why it isolates itself the same way first. A fixture
    that booted against the developer's real data directory would be a testing standards §9
    violation, and the first draft of this one was. The same ordering skips the autouse
    ``_deterministic_telemetry`` pin, so this fixture pins the identical snapshot itself:
    without it, the routing decision rendered on ``/routing/{decision_id}`` was made against
    the *developer's real GPU* — on this workstation (card mostly full) the hostile model was
    rejected ``insufficient_vram``, on a GPU-less machine (the CI runner, Docker) it was a
    second candidate, and the pages under test differed by machine.
    """
    import os

    from sweatmeter import GpuSample, TelemetryCollector, TelemetrySnapshot

    root = tmp_path_factory.mktemp("checklist")
    patch = pytest.MonkeyPatch()

    def snapshot(_self: TelemetryCollector) -> TelemetrySnapshot:
        return TelemetrySnapshot(
            timestamp=datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC),
            ram_available_bytes=64 * 1024**3,
            gpus=(
                GpuSample(
                    index=0,
                    vram_total_bytes=48 * 1024**3,
                    vram_used_bytes=1 * 1024**3,
                ),
            ),
        )

    patch.setattr(TelemetryCollector, "snapshot", snapshot)
    for name, directory in (("CONFIG", "config"), ("DATA", "data"), ("STATE", "state")):
        (root / directory).mkdir()
        patch.setenv(f"XDG_{name}_HOME", str(root / directory))
    patch.chdir(root)
    for key in list(os.environ):
        if key.startswith("LOADCOACH_"):
            patch.delenv(key, raising=False)
    patch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    application = bootstrap()
    url = application.loaded_settings.settings.storage.database_url
    assert url is not None
    with Database.from_url(url) as database, database.write() as session:
        session.add(
            Model(
                provider_kind="fake",
                provider_model_name=HOSTILE,
                artifact_digest=None,
                canonical_id=f"fake/{HOSTILE}@unknown",
                identity_confidence="name_only",
                first_seen_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
            )
        )
    with TestClient(application.app, base_url="http://localhost") as test_client:
        job_id = test_client.post(
            "/api/v1/jobs", json={"task": "general.chat", "prompt": HOSTILE}
        ).json()["job_id"]
        deadline = time.monotonic() + 10
        while (
            time.monotonic() < deadline
            and test_client.get(f"/api/v1/jobs/{job_id}").json()["state"] != "completed"
        ):
            time.sleep(0.05)
        test_client.post(
            f"/api/v1/jobs/{job_id}/feedback",
            json={"accepted": True, "notes": HOSTILE},
            headers={"X-Client-Name": "ideapress"},
        )
        test_client.app.state.checklist_job_id = job_id  # type: ignore[attr-defined]  # FastAPI
        yield test_client
    patch.undo()


def _pages(client: TestClient) -> dict[str, str]:
    job_id = client.app.state.checklist_job_id  # type: ignore[attr-defined]  # FastAPI
    decision_id = client.get(f"/api/v1/jobs/{job_id}").json()["routing"]["decision_id"]
    paths = [item["href"] for item in NAV_ITEMS] + [
        f"/jobs/{job_id}",
        f"/routing/{decision_id}",
        "/jobs?state=completed",
    ]
    pages = {}
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        pages[path] = response.text
    error = client.get("/jobs/01NOPE0000000000000000000", headers={"Accept": "text/html"})
    assert error.status_code == 404
    pages["/jobs/<missing>"] = error.text
    return pages


def test_every_page_has_the_shell_skip_link_main_nav_and_telemetry(client: TestClient) -> None:
    """§13: telemetry on every route; §7: semantic HTML, skip link, headings in order."""
    for path, html in _pages(client).items():
        assert 'class="skip-link" href="#content"' in html, path
        assert '<main id="content">' in html, path
        assert '<nav aria-label="Primary">' in html, path
        assert 'id="mw-telemetry-bar"' in html, path
        assert html.count("<h1>") == 1, path
        levels = [int(level) for level in re.findall(r"<h([1-6])[ >]", html)]
        for previous, current in zip(levels, levels[1:], strict=False):
            assert current <= previous + 1, (path, levels)  # never skip a heading level down
        assert '<html lang="en">' in html, path


def test_the_current_page_is_marked_in_the_navigation(client: TestClient) -> None:
    """§7: an assistive reader knows where it is."""
    for item in NAV_ITEMS:
        html = client.get(item["href"]).text
        assert f'href="{item["href"]}" aria-current="page"' in html, item


def test_every_form_control_has_a_label_and_every_button_is_a_button(client: TestClient) -> None:
    """§7: every form control has an associated label; real <button> elements."""
    for path, html in _pages(client).items():
        for control_id in re.findall(r'<(?:input|select|textarea)[^>]*\bid="([^"]+)"', html):
            hidden = re.search(rf'<input[^>]*type="hidden"[^>]*id="{re.escape(control_id)}"', html)
            if hidden:
                continue
            assert f'for="{control_id}"' in html, (path, control_id)
        assert 'role="button"' not in html, path
        for control in re.findall(r"<input[^>]*>", html):
            if 'type="hidden"' in control:
                continue
            assert 'id="' in control, (path, control)


def test_theme_choice_is_offered_on_every_page(client: TestClient) -> None:
    """§9: system, light, dark — the same information hierarchy in both themes is a token
    property (MirrorWall's), and the toggle is on every page."""
    for path, html in _pages(client).items():
        assert 'id="mw-theme-select"' in html and 'for="mw-theme-select"' in html, path
        assert 'data-theme-storage-key="loadcoach-theme"' in html, path


def test_tables_have_headers_scope_and_a_row_count(client: TestClient) -> None:
    """§5: dense tables with a row-count summary; header cells carry scope."""
    for path, html in _pages(client).items():
        tables = re.findall(r"<table[^>]*>.*?</table>", html, flags=re.S)
        for table in tables:
            assert '<th scope="col"' in table, path
        assert html.count("<table") <= html.count('class="row-count"') + html.count("<caption>"), (
            path
        )


def test_unsupported_values_are_dashes_with_a_reason_never_zero(client: TestClient) -> None:
    """§5, §13: `—` with a tooltip explaining why; never 0, never blank."""
    system = client.get("/system").text
    assert 'aria-label="Unavailable: not measurable in this environment"' in system
    reliability = client.get("/reliability").text
    assert "Unavailable: " in reliability  # a rate below its sample bound
    assert "unsupported" not in system.split("<main")[1].replace("unsupported_", "")


def test_colour_is_never_the_only_signal(client: TestClient) -> None:
    """§4.1, §13: every status badge carries a label."""
    for path, html in _pages(client).items():
        for badge in re.findall(r'<span class="badge"[^>]*>(.*?)</span>', html, flags=re.S):
            assert badge.strip(), path


def test_headline_metrics_link_to_their_raw_record(client: TestClient) -> None:
    """§5, §13: no headline metric is more than two interactions from its raw source."""
    dashboard = client.get("/").text
    assert 'href="/jobs/' in dashboard and 'href="/routing/' in dashboard
    assert 'href="/queue"' in dashboard and 'href="/models"' in dashboard
    job_id = client.app.state.checklist_job_id  # type: ignore[attr-defined]  # FastAPI
    job = client.get(f"/jobs/{job_id}").text
    assert f"/api/v1/jobs/{job_id}/explanation" in job  # the raw document, one click


def test_live_content_is_a_polite_live_region_and_the_page_is_complete_without_scripts(
    client: TestClient,
) -> None:
    """§7: live regions; §13: read-only content works with JavaScript disabled."""
    queue = client.get("/queue").text
    assert 'aria-live="polite"' in queue and 'role="status"' in queue
    assert "Executing now" in queue and "Circuit breakers" in queue  # rendered server-side
    assert "connecting…" in queue  # the loading state before the stream attaches


def test_no_page_loads_anything_from_the_network(client: TestClient) -> None:
    """§13: no network request leaves the machine — every asset is a same-origin path."""
    for path, html in _pages(client).items():
        for attribute in ("src", "href"):
            for target in re.findall(rf'{attribute}="([^"]+)"', html):
                assert not target.startswith(("http://", "https://", "//")), (path, target)


def test_application_templates_use_tokens_not_colours_or_small_text() -> None:
    """§1: no hard-coded colour; §13: metadata text never below 12 px; §10: no icon fonts."""
    for template in TEMPLATES.rglob("*.html"):
        source = template.read_text()
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", source.replace("#content", "")), template
        assert not re.search(r"font-size:\s*(?:[0-9]|1[01])px", source), template
        assert "fonts.googleapis" not in source and "cdn." not in source, template


def test_untrusted_content_is_escaped_on_every_page(client: TestClient) -> None:
    """Jinja autoescape on every page, with a hostile model name, prompt and note in the data."""
    for path, html in _pages(client).items():
        assert HOSTILE not in html, path
    assert HOSTILE not in client.get("/reliability").text


def test_every_view_has_empty_error_and_populated_states(client: TestClient) -> None:
    """§6, §13: the four states. Loading is the live region's; empty and populated are asserted
    per page by their own e2e tests; the error state is one page for every route."""
    error = client.get("/no-such-page", headers={"Accept": "text/html"})
    assert error.status_code == 404 and 'role="alert"' in error.text
    assert "NOT_FOUND" in error.text and error.headers["X-Request-ID"] in error.text
    assert "No job matches these filters" in client.get("/jobs?task=nothing.here").text


def test_what_a_person_must_still_check() -> None:
    """The §13 items no test here can hold, and what to do for each.

    * Layout at 1280×720 and at 375 px: open every navigation entry at both widths and confirm no
      primary control is clipped; tables scroll horizontally inside their own container. Check
      ``/jobs/{id}`` and ``/system`` first: both scrolled the page at 375 px (F6/F11, M5C-6 and
      M5C-11 — an escaped link blob and the 64-character fingerprint), fixed since MirrorWall
      0.2.1 by ``components.css``'s ``.kv-list dd`` ``overflow-wrap`` rule (the page-level
      stopgaps are deleted); confirm
      ``document.documentElement.scrollWidth <= window.innerWidth``.
    * Keyboard-only: tab through the Queue page's three controls, the Settings form and the
      Jobs filter bar; every focusable element shows a ring; Enter submits.
    * JavaScript disabled: the Queue page shows the report and the controls still work (the
      forms post without script); the telemetry bar shows dashes.
    * Browser network panel offline: no request leaves the machine (the same-origin test above
      is the static half of this).
    * Light and dark: switch the theme on the Dashboard and the explanation page; the hierarchy
      is the same and nothing is invisible.
    * Destructive operations: LoadCoach's UI has none (no delete, no restore); nothing to preview.
    """
    assert True
