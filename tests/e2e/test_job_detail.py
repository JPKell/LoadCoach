"""End-to-end: the Jobs pages and the explanation, rendered as an explanation (dev-plan P8).

P8 test 1: the explanation page shows every candidate, score, factor and rejection with its
numbers. And the page answers "why did it pick that model?" before it shows a table — the
narrative section is what a person reads first; the JSON viewer stays at the bottom as the raw
source (P8's named failure mode is a page that dumps JSON).
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from loadcoach.bootstrap import bootstrap


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    application = bootstrap()
    with TestClient(application.app, base_url="http://localhost") as test_client:
        yield test_client


def _run(client: TestClient, prompt: str = "hello", **extra: object) -> str:
    job_id = str(
        client.post(
            "/api/v1/jobs", json={"task": "general.chat", "prompt": prompt, **extra}
        ).json()["job_id"]
    )
    deadline = time.monotonic() + 10
    while (
        time.monotonic() < deadline
        and client.get(f"/api/v1/jobs/{job_id}").json()["state"] != "completed"
    ):
        time.sleep(0.05)
    assert client.get(f"/api/v1/jobs/{job_id}").json()["state"] == "completed"
    return job_id


def test_jobs_page_lists_a_job_and_the_detail_page_shows_its_history(client: TestClient) -> None:
    assert "No job has been submitted yet" in client.get("/jobs").text
    job_id = _run(client, "<script>alert(1)</script>")
    listing = client.get("/jobs")
    assert listing.status_code == 200
    assert job_id in listing.text
    assert "fake/" in listing.text  # the model column
    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert "completed" in detail.text
    assert "job.queued" in detail.text and "job.completed" in detail.text
    assert f"/api/v1/jobs/{job_id}/explanation" in detail.text
    # Untrusted output is escaped, while the shell's own script stays.
    assert "<script>alert(1)</script>" not in detail.text
    assert client.get("/jobs/01NOPE0000000000000000000").status_code == 404


def test_the_job_page_answers_why_before_any_table_and_lists_feedback(client: TestClient) -> None:
    job_id = _run(client)
    assert "No caller has given feedback" in client.get(f"/jobs/{job_id}").text
    client.post(
        f"/api/v1/jobs/{job_id}/feedback",
        json={"accepted": True, "quality_score": 0.9, "notes": "used verbatim"},
        headers={"X-Client-Name": "ideapress"},
    )
    page = client.get(f"/jobs/{job_id}").text
    why = page.index("Why this model")
    assert why < page.index("What carried the score") < page.index("<h3>Attempts</h3>")
    assert "Selected fake/" in page and "the only eligible candidate" in page
    assert "task fit " in page and "× reliability " in page
    assert "low_evidence" in page and "declared flags and priors" in page
    decision_id = client.get(f"/api/v1/jobs/{job_id}").json()["routing"]["decision_id"]
    assert f'href="/routing/{decision_id}"' in page
    assert "ideapress" in page and "used verbatim" in page and "0.90" in page


def test_explanation_page_shows_every_candidate_score_factor_and_rejection_with_numbers(
    client: TestClient,
) -> None:
    """P8 test 1, with a rejection: the fake model is excluded by name, so nothing is eligible."""
    job_id = _run(client)
    document = client.get(f"/api/v1/jobs/{job_id}").json()
    canonical = document["model"]["canonical_id"]
    explanation = client.get(f"/api/v1/jobs/{job_id}/explanation").json()
    page = client.get(f"/routing/{explanation['decision_id']}")
    assert page.status_code == 200
    text = page.text
    assert "Why this model" in text
    assert text.index("Why this model") < text.index("The complete stored explanation")
    for candidate in explanation["candidates"]:
        assert candidate["canonical_id"] in text
        assert f"{candidate['task_fit']:.4f}" in text
        assert f"{candidate['final_score']:.4f}" in text
        for factor in ("reliability", "availability", "residency", "cost"):
            assert str(candidate["factors"][factor]) in text
        for entry in candidate["capabilities"]:
            assert entry["capability"] in text
            assert entry["source"] in text
    assert "What carried the score" in text
    assert "Contribution is weight × score × confidence" in text

    refused = client.post(
        "/api/v1/route",
        json={"task": "general.chat", "constraints": {"exclude_models": [canonical]}},
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "NO_ELIGIBLE_MODEL"
    decision_id = refused.json()["error"]["details"]["decision_id"]
    rejected_page = client.get(f"/routing/{decision_id}").text
    assert "No candidate satisfied" in rejected_page
    assert "Why the others were set aside" in rejected_page
    assert "excluded_by_policy" in rejected_page
    assert "excludes it by name" in rejected_page
    assert "exclude_models" in rejected_page  # the numbers behind the rejection
    assert client.get("/routing/01NOPE0000000000000000000").status_code == 404


def test_jobs_page_filters_and_paginates(client: TestClient) -> None:
    for _ in range(3):
        _run(client)
    assert "No job matches these filters" in client.get("/jobs", params={"state": "failed"}).text
    filtered = client.get("/jobs", params={"state": "completed", "class": "normal"}).text
    assert "3 rows" in filtered and 'aria-label="Pagination"' in filtered
    assert "No job matches" not in client.get("/jobs", params={"source": "anonymous"}).text
    assert (
        "No job matches these filters" in client.get("/jobs", params={"task": "code.review"}).text
    )
    unknown_state = client.get("/jobs", params={"state": "bogus"})
    assert unknown_state.status_code == 200 and "No job matches" in unknown_state.text


def test_queue_page_renders_the_report(client: TestClient) -> None:
    page = client.get("/queue")
    assert page.status_code == 200
    assert "Active jobs" in page.text
    assert "No job is executing" in page.text
    assert "Circuit breakers" in page.text
