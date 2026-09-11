"""ADR-0139, as this application inherits it at row WM2: a page loads at most 120 KB of
JavaScript in total, ECharts and mermaid aside. Marked ``performance`` like every budget."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from loadcoach.bootstrap import bootstrap

pytestmark = pytest.mark.performance

_SCRIPT_SRC = re.compile(r'<script[^>]+src="([^"]+)"')
_INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def test_every_page_stays_under_the_total_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.startswith("LOADCOACH_"):
            monkeypatch.delenv(name, raising=False)
    for name, directory in (("CONFIG", "config"), ("DATA", "data"), ("STATE", "state")):
        (tmp_path / directory).mkdir(exist_ok=True)
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / directory))
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    sizes: dict[str, int] = {}
    totals: dict[str, int] = {}
    with TestClient(bootstrap().app, base_url="http://127.0.0.1") as client:
        job_id = client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "x"}).json()[
            "job_id"
        ]
        for path in ("/", "/jobs", f"/jobs/{job_id}", "/models", "/queue", "/system", "/settings"):
            html = client.get(path).text
            total = sum(len(script) for script in _INLINE.findall(html))
            for src in _SCRIPT_SRC.findall(html):
                if "vendor/echarts" in src or "vendor/mermaid" in src:
                    continue
                name = src.split("?")[0]
                if name not in sizes:
                    asset = client.get(src)
                    assert asset.status_code == 200, (path, src)
                    sizes[name] = len(asset.content)
                total += sizes[name]
            totals[path] = total
    assert max(totals.values()) <= 120 * 1024, totals
