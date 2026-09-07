"""``loadcoach doctor`` diagnoses every failure mode the troubleshooting guide lists (M9 audit
Group 5, item D6 — ported from FreeWeight's `test_troubleshooting_covers_doctor.py`).

Unlike FreeWeight's component-based health report, `loadcoach doctor` reports named *codes*
(`loadcoach.services.doctor.DOCUMENTED_FAILURE_MODES`) — the ordinary spec §13 codes, plus a
`degraded:<component>` family for the health components that are never a failure on their own
(provider, evidence, queue, reliability, telemetry). This test holds the guide to the same promise
FreeWeight's does: every code the doctor can report is named in `docs/troubleshooting.md`, either
verbatim (an ordinary code) or by its component name (a `degraded:` code) — so a check added to
the doctor without a line in the guide fails here, before dev-plan P9's own last test would have
found it by other means.
"""

from __future__ import annotations

from pathlib import Path

from loadcoach.services.doctor import DOCUMENTED_FAILURE_MODES

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "troubleshooting.md"


def _guide_text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def test_every_code_the_doctor_can_report_is_named_in_the_guide() -> None:
    text = _guide_text()
    lowered = text.lower()
    missing: list[str] = []
    for code in DOCUMENTED_FAILURE_MODES:
        if code.startswith("degraded:"):
            component = code.removeprefix("degraded:")
            if component not in lowered:
                missing.append(code)
        elif f"`{code}`" not in text:
            missing.append(code)
    assert missing == [], f"codes the doctor can report but the guide does not name: {missing}"
