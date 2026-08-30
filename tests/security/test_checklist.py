"""Security Standards §14, item by item (dev-plan P9 unit 10).

Every bullet of §14 is either held here, held by a named test elsewhere in this repository (the
map at the bottom asserts those tests exist), or does not apply to LoadCoach for a reason stated
beside it. "Does not apply" is a claim about the surface, so it is asserted about the surface —
no endpoint accepts a path or an archive — rather than merely written down.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from baseaicore import ValidationError
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from modelrack.testing import FakeGeneration, FakeProvider, FakeScript, FakeToolCall
from tests.integration.test_generate import NOW, _model
from tests.integration.test_jobs_api import _client, _wait

from loadcoach.config import (
    InsecureBindingError,
    ProviderSettings,
    ServerSettings,
    Settings,
    StorageSettings,
    load_settings,
)
from loadcoach.infrastructure.db.models import ApiToken, Job
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.execution import load_task_schema
from loadcoach.services.models import discover_models
from loadcoach.services.task_profiles import (
    DEFAULT_SCHEMAS_DIR,
    import_task_profiles,
    read_task_profiles_file,
)
from loadcoach.web.app import create_app

HOSTILE_OUTPUT = (
    "{{ 7 * 7 }} <script>alert('x')</script> ../../etc/passwd '; DROP TABLE jobs; -- \\x00"
)


@pytest.fixture
def tokened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str]]:
    """A loopback server with one read token and one revoked token."""
    url = f"sqlite:///{tmp_path / 'sec.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    settings = load_settings().settings
    seed = Database.from_url(url)
    ensure_ready(seed, auto_migrate=True)
    with seed.write() as session:
        session.add(
            ApiToken(
                name="reader",
                token_sha256=hashlib.sha256(b"read-token").hexdigest(),
                scope="read",
                created_at=NOW,
            )
        )
        session.add(
            ApiToken(
                name="gone",
                token_sha256=hashlib.sha256(b"revoked-token").hexdigest(),
                scope="admin",
                created_at=NOW,
                revoked_at=NOW,
            )
        )
    seed.close()
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        yield client, url


# §14: path traversal rejected for every path-accepting endpoint and CLI argument


def test_no_http_endpoint_accepts_a_filesystem_path() -> None:
    """The one path-shaped input is a task profile's schema reference, resolved by the server."""
    app = create_app(Settings(provider=ProviderSettings(kind="fake")))
    suspicious = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for parameter in route.dependant.query_params + route.dependant.path_params:
            if any(word in parameter.name for word in ("path", "file", "dir")):
                suspicious.append((route.path, parameter.name))
        for body_field in route.dependant.body_params:
            annotation = getattr(body_field, "type_", None) or body_field.field_info.annotation
            fields = getattr(annotation, "model_fields", {}) or {}
            for name in fields:
                if any(word in name for word in ("path", "file", "dir")):
                    suspicious.append((route.path, name))
    assert suspicious == []


def test_a_schema_reference_cannot_escape_the_schemas_directory(tmp_path: Path) -> None:
    outside = tmp_path / "secret.json"
    outside.write_text('{"stolen": true}')
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "ok.json").write_text('{"type": "object"}')
    assert load_task_schema("ok.json", schemas_dir=schemas) == {"type": "object"}
    for reference in ("../secret.json", "sub/../../secret.json", str(outside)):
        with pytest.raises(ValidationError, match="outside the schemas directory"):
            load_task_schema(reference, schemas_dir=schemas)
    assert load_task_schema("code_review_findings.json", schemas_dir=DEFAULT_SCHEMAS_DIR)


# §14: oversize body rejected before buffering


def test_an_oversize_body_is_413_before_it_is_read(tmp_path: Path) -> None:
    settings = Settings(
        server=ServerSettings(max_body_bytes=1024),
        storage=StorageSettings(database_url=f"sqlite:///{tmp_path / 'big.sqlite3'}"),
        provider=ProviderSettings(kind="fake"),
    )
    seed = Database.from_url(settings.storage.database_url or "")
    ensure_ready(seed, auto_migrate=True)
    seed.close()
    with TestClient(create_app(settings), base_url="http://localhost") as client:
        declared = client.post(
            "/api/v1/jobs",
            content=b"x" * 4096,
            headers={"Content-Type": "application/json", "Content-Length": "4096"},
        )
        assert declared.status_code == 413
        assert declared.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"

        def chunks() -> Iterator[bytes]:
            for _ in range(8):
                yield b"y" * 512

        streamed = client.post(
            "/api/v1/jobs",
            content=chunks(),
            headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
        )
        assert streamed.status_code == 413
        small = client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "fits"})
        assert small.status_code in (202, 422, 404)  # parsed, whatever routing then says


# §14: authenticated endpoints reject missing, malformed, revoked and wrong-scope tokens


def test_missing_malformed_revoked_and_wrong_scope_tokens_are_refused(
    tokened: tuple[TestClient, str],
) -> None:
    client, _ = tokened
    assert client.get("/api/v1/health").status_code == 401  # missing
    for malformed in ("Token read-token", "Bearer", "Bearer ", "read-token", "Basic cmVhZA=="):
        response = client.get("/api/v1/health", headers={"Authorization": malformed})
        assert response.status_code == 401, malformed
        assert response.json()["error"]["code"] == "UNAUTHORIZED"
    revoked = client.get("/api/v1/health", headers={"Authorization": "Bearer revoked-token"})
    assert revoked.status_code == 401
    wrong_scope = client.post("/api/v1/queue/pause", headers={"Authorization": "Bearer read-token"})
    assert wrong_scope.status_code == 403
    assert wrong_scope.json()["error"]["code"] == "FORBIDDEN"
    assert (
        client.get("/api/v1/health", headers={"Authorization": "Bearer read-token"}).status_code
        == 200
    )


# §14: token comparison is constant-time; the stored value is a hash, asserted on the row


def test_the_row_holds_a_hash_and_the_comparison_is_constant_time(
    tokened: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, url = tokened
    database = Database.from_url(url)
    try:
        with database.read() as session:
            row = session.query(ApiToken).filter_by(name="reader").one()
            assert row.token_sha256 == hashlib.sha256(b"read-token").hexdigest()
            assert "read-token" not in row.token_sha256 and len(row.token_sha256) == 64
    finally:
        database.close()
    calls: list[tuple[str, str]] = []
    original = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((str(a), str(b)))
        return bool(original(a, b))

    monkeypatch.setattr("loadcoach.web.auth.hmac.compare_digest", spy)
    assert (
        client.get("/api/v1/health", headers={"Authorization": "Bearer read-token"}).status_code
        == 200
    )
    assert calls, "the lookup did not go through hmac.compare_digest"
    assert all("read-token" not in a and "read-token" not in b for a, b in calls)


# §14: log output contains no secret for a request that carried one


def test_logs_never_contain_a_presented_token(
    tokened: tuple[TestClient, str], caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = tokened
    caplog.set_level(logging.DEBUG)
    client.get("/api/v1/health", headers={"Authorization": "Bearer read-token"})
    client.get("/api/v1/health", headers={"Authorization": "Bearer wrong-secret-value"})
    client.post("/api/v1/queue/pause", headers={"Authorization": "Bearer read-token"})
    text = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "read-token" not in text and "wrong-secret-value" not in text


# §14: archives; sandbox — do not apply, and the surface proves it


def test_no_endpoint_or_command_accepts_an_archive_and_no_tool_call_is_ever_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LoadCoach imports JSON envelopes, never archives, and passes tool calls back untouched.

    The sandbox item is FreeWeight's (code-execution benchmarks); LoadCoach's counterpart is spec
    §14's "never executes a tool call": a provider that requests one gets it returned to the
    caller, and nothing on this machine runs.
    """
    import subprocess

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("LoadCoach must never spawn a process for a tool call")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    # No archive handling anywhere in the application: nothing to bomb, nothing to traverse.
    package = Path(importlib.import_module("loadcoach").__file__ or "").parent
    for module in package.rglob("*.py"):
        text = module.read_text()
        assert "zipfile" not in text and "tarfile" not in text, module
        assert "unpack_archive" not in text, module

    script = FakeScript(
        models=(_model(),),
        generations=(
            FakeGeneration(
                text="calling a tool",
                tool_calls=(FakeToolCall(name="shell", arguments={"cmd": "rm -rf /"}),),
            ),
        ),
        repeat_final_generation=True,
    )
    url = f"sqlite:///{tmp_path / 'tools.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    settings = load_settings().settings
    database = Database.from_url(url)
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    discover_models(database, FakeProvider(script), now=NOW)
    database.close()
    app = create_app(settings)
    with TestClient(app, base_url="http://localhost") as client:
        provider = FakeProvider(script)
        app.state.provider = provider
        app.state.queue_runtime.replace_provider(provider)
        response = client.post("/api/v1/generate", json={"task": "general.chat", "prompt": "go"})
        assert response.status_code == 200, response.text
        # Tool calls are returned as the streamed fragments the provider produced — the name,
        # then the argument text — and nothing on this machine ran them.
        fragments = response.json()["output"]["tool_calls"]
        assert fragments and fragments[0]["name"] == "shell"
        arguments = "".join(f["arguments_fragment"] or "" for f in fragments)
        assert json.loads(arguments) == {"cmd": "rm -rf /"}


# §14: model output containing template, script, traversal and SQL text is inert


def test_hostile_model_output_is_stored_verbatim_and_rendered_without_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = FakeScript(
        models=(_model(),),
        generations=(FakeGeneration(text=HOSTILE_OUTPUT),),
        repeat_final_generation=True,
    )
    url = f"sqlite:///{tmp_path / 'hostile.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    settings = load_settings().settings
    database = Database.from_url(url)
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    discover_models(database, FakeProvider(script), now=NOW)
    database.close()
    app = create_app(settings)
    with TestClient(app, base_url="http://localhost") as client:
        provider = FakeProvider(script)
        app.state.provider = provider
        app.state.queue_runtime.replace_provider(provider)
        job_id = client.post(
            "/api/v1/jobs", json={"task": "general.chat", "prompt": HOSTILE_OUTPUT}
        ).json()["job_id"]
        document = _wait(client, job_id)
        assert document["output"]["text"] == HOSTILE_OUTPUT  # verbatim over the API
        page = client.get(f"/jobs/{job_id}").text
        assert "<script>alert('x')</script>" not in page  # escaped
        assert "49" not in page.split("Output")[1].split("</pre>")[0]  # not templated
        assert "{{ 7 * 7 }}" in page.replace("&#39;", "'").replace("&#34;", '"')
        api_page = client.get("/api/v1/jobs", params={"state": "completed"}).json()
        assert api_page["items"][0]["output"]["text"] == HOSTILE_OUTPUT
        database = Database.from_url(url)
        try:
            with database.read() as session:
                stored = session.get(Job, job_id)
                assert stored is not None and stored.response_text == HOSTILE_OUTPUT
                assert session.query(Job).count() == 1  # the table is still there
        finally:
            database.close()


# §14: a cross-origin JSON post is rejected; a forged form is (elsewhere); a valid one succeeds


def test_a_cross_origin_json_post_is_refused_and_a_same_origin_one_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        body = {"task": "general.chat", "prompt": "hello"}
        foreign = client.post("/api/v1/jobs", json=body, headers={"Origin": "http://evil.example"})
        assert foreign.status_code == 403
        assert foreign.json()["error"]["code"] == "CSRF_FAILED"
        null_origin = client.post("/api/v1/jobs", json=body, headers={"Origin": "null"})
        assert null_origin.status_code == 403
        same = client.post("/api/v1/jobs", json=body, headers={"Origin": "http://localhost"})
        assert same.status_code == 202
        scripted = client.post("/api/v1/jobs", json=body)  # no Origin: a script or IdeaPress
        assert scripted.status_code == 202
        assert (
            client.get("/api/v1/jobs", headers={"Origin": "http://evil.example"}).status_code == 200
        )


# §14: a non-loopback bind without allowed_hosts refuses to start, like the missing-token case


def test_a_non_loopback_bind_without_allowed_hosts_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loadcoach.bootstrap import bootstrap

    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'h.sqlite3'}")
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    monkeypatch.setenv("LOADCOACH_SERVER__HOST", "192.0.2.10")
    with pytest.raises(InsecureBindingError, match="allowed_hosts"):
        bootstrap()


# §14: the map of every remaining bullet to the test that holds it


CHECKLIST_MAP: dict[str, tuple[str, str]] = {
    "non-loopback bind without a token refuses to start": (
        "tests.security.test_scopes",
        "test_a_non_loopback_bind_without_tokens_refuses_to_start",
    ),
    "unexpected Host is 421 on both binds, before authentication": (
        "tests.security.test_scopes",
        "test_host_validation_precedes_authentication_on_both_binds",
    ),
    "a forged HTML form post is CSRF_FAILED and a valid one succeeds": (
        "tests.e2e.test_queue_controls",
        "test_the_queue_page_has_working_controls_behind_csrf",
    ),
    "/version answers without a credential while /health does not": (
        "tests.security.test_scopes",
        "test_scoped_endpoints_reject_wrong_scopes_and_accept_right_ones_over_http",
    ),
    "evidence import refuses a file:// URL": (
        "tests.integration.test_evidence_fetch",
        "test_a_file_url_is_refused",
    ),
    "evidence import refuses a host outside the allowlist": (
        "tests.integration.test_evidence_fetch",
        "test_a_host_outside_the_allowlist_is_refused",
    ),
    "evidence import refuses a literal link-local address": (
        "tests.integration.test_evidence_fetch",
        "test_a_literal_link_local_address_is_refused_even_when_allowlisted",
    ),
    "evidence import refuses a redirect that changes host": (
        "tests.integration.test_evidence_fetch",
        "test_a_cross_host_redirect_is_refused",
    ),
    "evidence import refuses a response over the cap before parsing": (
        "tests.integration.test_evidence_fetch",
        "test_an_oversize_body_is_refused_and_the_transfer_is_stopped_early",
    ),
    "no evidence credential is sent to another host": (
        "tests.integration.test_evidence_fetch",
        "test_a_credential_configured_for_one_source_is_never_sent_to_another",
    ),
    "no evidence credential is forwarded across a redirect": (
        "tests.integration.test_evidence_fetch",
        "test_no_credential_is_forwarded_across_a_redirect",
    ),
    "oversize evidence bundle in the body is refused": (
        "tests.integration.test_evidence_api",
        "test_an_oversize_import_is_rejected",
    ),
}


def test_every_remaining_checklist_item_is_held_by_a_named_test() -> None:
    for item, (module_name, function_name) in CHECKLIST_MAP.items():
        module = importlib.import_module(module_name)
        assert callable(getattr(module, function_name, None)), (item, module_name, function_name)
    assert time.time() > 0  # the map is data; the assertion above is the test
    assert json.dumps(sorted(CHECKLIST_MAP))  # and it is serializable for the report
