"""The per-source queue-depth cap over HTTP: one caller cannot fill the queue (spec §14)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.test_jobs_api import _client


def test_a_source_past_its_cap_is_refused_with_queue_full_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(
        tmp_path, monkeypatch, env={"LOADCOACH_QUEUE__MAX_ACTIVE_PER_SOURCE": "2"}
    ) as client:
        assert client.post("/api/v1/queue/pause").status_code == 202  # hold them in the queue
        body = {"task": "general.chat", "prompt": "hello", "class": "background"}
        for _ in range(2):
            assert (
                client.post(
                    "/api/v1/jobs", json=body, headers={"X-Client-Name": "ideapress"}
                ).status_code
                == 202
            )
        refused = client.post("/api/v1/jobs", json=body, headers={"X-Client-Name": "ideapress"})
        assert refused.status_code == 429
        error = refused.json()["error"]
        assert error["code"] == "QUEUE_FULL"
        assert error["details"] == {"source": "ideapress", "active": 2, "max_active_per_source": 2}
        # Another caller is not starved by the first one's cap.
        assert (
            client.post(
                "/api/v1/jobs", json=body, headers={"X-Client-Name": "reviewer"}
            ).status_code
            == 202
        )
        assert client.post("/api/v1/queue/resume").status_code == 202
