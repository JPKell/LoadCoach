"""Scopes in the service layer **and** at the route (api.md §11, ADR-0014 §5; dev-plan P9).

P9's named failure mode is a scope checked at the route but not in the service. The first test
here is the one that catches it: every mutating service is called directly, with no HTTP in the
way, holding a read-scoped principal, and must refuse before touching anything. The route table
test is the structural half — every API route except ``/version`` declares the principal — and
the HTTP tests are the outer check.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from modelrack.testing import FakeProvider, FakeScript
from tests.integration.test_generate import NOW, _model

from loadcoach.config import (
    ExecutionSettings,
    InsecureBindingError,
    ProviderSettings,
    QueueSettings,
    ServerSettings,
    Settings,
    StorageSettings,
    load_settings,
)
from loadcoach.domain.authorization import LOCAL, InsufficientScope, Principal, authorize
from loadcoach.domain.queue_state import JobState
from loadcoach.infrastructure.db.models import ApiToken, Job
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.evidence import import_bundle
from loadcoach.services.feedback import FeedbackSubmission, record_feedback
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.models import discover_models
from loadcoach.services.queue import JobSubmission, cancel_job, enqueue, set_queue_flag
from loadcoach.services.settings import write_runtime_settings
from loadcoach.services.task_profiles import (
    DEFAULT_SCHEMAS_DIR,
    import_task_profiles,
    read_task_profiles_file,
)
from loadcoach.web.app import create_app

READER = Principal(name="reader", scope="read", source="token")
WRITER = Principal(name="writer", scope="write", source="token")
ADMIN = Principal(name="root", scope="admin", source="token")


# ------------------------------------------------------------------------ the rule itself


def test_scopes_are_cumulative_and_none_means_an_internal_caller() -> None:
    assert READER.grants("read") and not READER.grants("write")
    assert WRITER.grants("read") and WRITER.grants("write") and not WRITER.grants("admin")
    assert ADMIN.grants("admin") and LOCAL.grants("admin")
    assert authorize(None, "admin") is None
    assert authorize(WRITER, "write") is WRITER
    with pytest.raises(InsufficientScope) as refused:
        authorize(READER, "write")
    assert refused.value.code == "FORBIDDEN"
    assert refused.value.details == {
        "required_scope": "write",
        "token_scope": "read",
        "principal": "reader",
    }
    with pytest.raises(ValueError, match="unknown scope"):
        authorize(ADMIN, "root")


# --------------------------------------------------------------- the service-layer check


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    handle = Database.from_url(f"sqlite:///{tmp_path / 'scopes.sqlite3'}")
    ensure_ready(handle, auto_migrate=True)
    import_task_profiles(handle, read_task_profiles_file(), now=NOW)
    discover_models(handle, FakeProvider(FakeScript(models=(_model(),))), now=NOW)
    try:
        yield handle
    finally:
        handle.close()


def _settings() -> Settings:
    return Settings(provider=ProviderSettings(kind="fake"))


def test_every_mutating_service_refuses_a_read_scoped_principal_before_touching_anything(
    database: Database,
) -> None:
    """The call an internal caller will one day make, made now, with the wrong scope."""
    settings = _settings()
    sink = JobEventSink()
    refusals: dict[str, Any] = {}

    def refused(name: str, call: Any) -> None:
        with pytest.raises(InsufficientScope) as caught:
            call()
        refusals[name] = caught.value.details["required_scope"]

    refused(
        "enqueue",
        lambda: enqueue(
            database,
            JobSubmission(task="general.chat", prompt="x"),
            now=NOW,
            queue_settings=settings.queue,
            execution_settings=settings.execution,
            sink=sink,
            principal=READER,
        ),
    )
    refused("cancel_job", lambda: cancel_job(database, sink, "01X", now=NOW, principal=READER))
    refused(
        "record_feedback",
        lambda: record_feedback(
            database,
            "01X",
            FeedbackSubmission(source="reader", accepted=True),
            now=NOW,
            principal=READER,
        ),
    )
    refused(
        "set_queue_flag",
        lambda: set_queue_flag(database, "queue.paused", True, now=NOW, principal=WRITER),
    )
    refused(
        "write_runtime_settings",
        lambda: write_runtime_settings(
            database, {"queue.paused": True}, settings=settings, now=NOW, principal=WRITER
        ),
    )
    refused(
        "import_bundle",
        lambda: import_bundle(database, "{}", now=NOW, accept_schema_majors=[1], principal=WRITER),
    )
    from tests.integration.test_evidence_routing_change import _facts

    from loadcoach.services.routing import RouteRequest, RoutingPolicy, route

    refused(
        "route",
        lambda: route(
            database,
            RouteRequest(task="general.chat"),
            provider=_facts(),
            policy=RoutingPolicy(),
            now=NOW,
            principal=READER,
        ),
    )
    from loadcoach.services.execution import ExecutionContext, GenerateRequest, execute

    refused(
        "execute",
        lambda: execute(
            database,
            GenerateRequest(task="general.chat", prompt="x", source="reader"),
            ExecutionContext(
                provider=FakeProvider(FakeScript(models=(_model(),))),
                provider_facts=_facts(),
                policy=RoutingPolicy(),
                schemas_dir=DEFAULT_SCHEMAS_DIR,
                sink=sink,
            ),
            principal=READER,
        ),
    )
    assert refusals == {
        "enqueue": "write",
        "cancel_job": "write",
        "record_feedback": "write",
        "set_queue_flag": "admin",
        "write_runtime_settings": "admin",
        "import_bundle": "admin",
        "route": "write",
        "execute": "write",
    }
    # Nothing was written by any of them.
    with database.read() as session:
        assert session.query(Job).count() == 0
    with database.read() as session:
        from loadcoach.infrastructure.db.models import RoutingDecision, Setting

        assert session.query(RoutingDecision).count() == 0
        assert session.query(Setting).count() == 0


def test_a_sufficient_principal_and_an_internal_caller_both_pass(database: Database) -> None:
    settings = _settings()
    sink = JobEventSink()
    accepted = enqueue(
        database,
        JobSubmission(task="general.chat", prompt="x", source="writer"),
        now=NOW,
        queue_settings=settings.queue,
        execution_settings=settings.execution,
        sink=sink,
        principal=WRITER,
    )
    assert accepted.created
    internal = enqueue(
        database,
        JobSubmission(task="general.chat", prompt="y", source="cli"),
        now=NOW,
        queue_settings=settings.queue,
        execution_settings=settings.execution,
        sink=sink,
        principal=LOCAL,
    )
    assert internal.created
    set_queue_flag(database, "queue.paused", True, now=NOW, principal=ADMIN)
    set_queue_flag(database, "queue.paused", False, now=NOW)  # no request behind it: allowed


# ------------------------------------------------------------------ the route-table check


def test_every_api_route_except_version_declares_the_principal() -> None:
    """The structural half: a route that forgot the dependency cannot pass a principal on."""
    app = create_app(_settings())
    missing = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path == "/api/v1/version" or route.path.startswith("/token-cookie"):
            continue
        names = {p.name for p in route.dependant.query_params + route.dependant.path_params}
        dependencies = [d.call.__name__ for d in route.dependant.dependencies if d.call]
        if "authenticate" not in dependencies:
            missing.append((sorted(route.methods or ()), route.path, sorted(names)))
    assert not missing, missing


# --------------------------------------------------------------------- over HTTP, tokened


def _tokened_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    url = f"sqlite:///{tmp_path / 'tokened.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    settings = load_settings().settings
    seed = Database.from_url(url)
    ensure_ready(seed, auto_migrate=True)
    import_task_profiles(seed, read_task_profiles_file(), now=NOW)
    discover_models(seed, FakeProvider(FakeScript(models=(_model(),))), now=NOW)
    with seed.write() as session:
        for name, raw, scope in (
            ("reader", "read-token", "read"),
            ("writer", "write-token", "write"),
            ("root", "admin-token", "admin"),
        ):
            session.add(
                ApiToken(
                    name=name,
                    token_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                    scope=scope,
                    created_at=NOW,
                )
            )
    seed.close()
    return TestClient(create_app(settings), base_url="http://localhost")


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def test_scoped_endpoints_reject_wrong_scopes_and_accept_right_ones_over_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dev-plan P9 Tests: scoped endpoints reject wrong scopes."""
    with _tokened_client(tmp_path, monkeypatch) as client:
        # Reads need read; /health is read when auth is on (ADR-0026 §5); /version needs nothing.
        assert client.get("/api/v1/version").status_code == 200
        assert client.get("/api/v1/health").status_code == 401
        assert client.get("/api/v1/health", headers=_auth("nope")).status_code == 401
        assert client.get("/api/v1/health", headers=_auth("read-token")).status_code == 200
        assert client.get("/api/v1/models", headers=_auth("read-token")).status_code == 200
        # Writes need write.
        body = {"task": "general.chat", "prompt": "hello"}
        refused = client.post("/api/v1/jobs", json=body, headers=_auth("read-token"))
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "FORBIDDEN"
        assert refused.json()["error"]["details"]["required_scope"] == "write"
        assert (
            client.post(
                "/api/v1/route", json={"task": "general.chat"}, headers=_auth("read-token")
            ).status_code
            == 403
        )
        accepted = client.post("/api/v1/jobs", json=body, headers=_auth("write-token"))
        assert accepted.status_code == 202 and accepted.json()["source"] == "writer"
        # Admin needs admin.
        assert client.post("/api/v1/queue/pause", headers=_auth("write-token")).status_code == 403
        assert (
            client.put(
                "/api/v1/settings", json={"queue.paused": False}, headers=_auth("write-token")
            ).status_code
            == 403
        )
        assert client.post("/api/v1/queue/pause", headers=_auth("admin-token")).status_code == 202
        assert client.post("/api/v1/queue/resume", headers=_auth("admin-token")).status_code == 202
        # The UI needs the token too, and accepts it as the cookie.
        assert client.get("/", headers={"Accept": "text/html"}).status_code == 401
        page = client.get("/", headers={"Accept": "text/html"})
        assert 'action="/token-cookie"' in page.text  # the 401 page offers to carry a token
        # F5 (M5C-5): the form's cookies are Secure, so the page says where the flow works.
        assert "HTTPS or loopback" in page.text
        assert client.get("/", headers={"Cookie": "loadcoach_token=read-token"}).status_code == 200
        assert client.get("/queue", headers={"Cookie": "loadcoach_token=nope"}).status_code == 401


def test_a_non_loopback_bind_without_a_proxy_warns_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """F5 (M5C-5) / ADR-0014 §7: plain HTTP beyond loopback, no evidence of a proxy — warn.

    `trusted_proxies` being configured is the evidence; without it the warning says what breaks
    (the UI's Secure cookies) and what is unaffected (the bearer-token API). The warning must
    not fire on loopback or once a proxy is declared.
    """
    import logging

    from typer.testing import CliRunner

    from loadcoach.bootstrap import bootstrap
    from loadcoach.cli.main import app as cli

    url = f"sqlite:///{tmp_path / 'warn.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    seed = Database.from_url(url)
    ensure_ready(seed, auto_migrate=True)
    seed.close()
    # bootstrap()'s configure_logging replaces the root handlers, which would silently remove
    # caplog's capturing handler; the warning under test is emitted after it runs.
    monkeypatch.setattr("loadcoach.bootstrap.configure_logging", lambda *a, **k: None)
    assert CliRunner().invoke(cli, ["token", "create", "ops", "--scope", "admin"]).exit_code == 0
    monkeypatch.setenv("LOADCOACH_SERVER__HOST", "192.0.2.10")
    monkeypatch.setenv("LOADCOACH_SERVER__ALLOWED_HOSTS", "coach.test")
    with caplog.at_level(logging.WARNING, logger="loadcoach.bootstrap"):
        bootstrap()
    exposure = [r for r in caplog.records if r.message == "server.plain_http_exposure"]
    assert len(exposure) == 1
    assert "HTTPS or loopback" in exposure[0].detail  # type: ignore[attr-defined]  # extra

    caplog.clear()
    monkeypatch.setenv("LOADCOACH_SERVER__TRUSTED_PROXIES", "127.0.0.0/8")
    with caplog.at_level(logging.WARNING, logger="loadcoach.bootstrap"):
        bootstrap()
    assert not [r for r in caplog.records if r.message == "server.plain_http_exposure"]


def test_host_validation_precedes_authentication_on_both_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security Standards §14: a bad Host and a bad token is 421, not 401 — on loopback and not."""
    with _tokened_client(tmp_path, monkeypatch) as client:
        response = client.get("/api/v1/health", headers={"Host": "evil.example", **_auth("nope")})
        assert response.status_code == 421
        assert response.json()["error"]["code"] == "MISDIRECTED_REQUEST"

    lan = Settings(
        server=ServerSettings(host="192.0.2.10", allowed_hosts=("coach.test",)),
        storage=StorageSettings(database_url=f"sqlite:///{tmp_path / 'tokened.sqlite3'}"),
        provider=ProviderSettings(kind="fake"),
    )
    with TestClient(create_app(lan), base_url="http://coach.test") as client:
        rebinding = client.get("/api/v1/health", headers={"Host": "evil.example", **_auth("nope")})
        assert rebinding.status_code == 421
        assert client.get("/api/v1/health", headers=_auth("nope")).status_code == 401
        assert client.get("/api/v1/health", headers=_auth("read-token")).status_code == 200
        assert client.get("/api/v1/version").status_code == 200


def test_a_non_loopback_bind_without_tokens_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dev-plan P9 Tests; ADR-0014 §2. Then, with one token, it starts."""
    from typer.testing import CliRunner

    from loadcoach.bootstrap import bootstrap
    from loadcoach.cli.main import app as cli

    url = f"sqlite:///{tmp_path / 'lan.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    monkeypatch.setenv("LOADCOACH_SERVER__HOST", "192.0.2.10")
    monkeypatch.setenv("LOADCOACH_SERVER__ALLOWED_HOSTS", "coach.test")
    with pytest.raises(InsecureBindingError) as refused:
        bootstrap()
    assert refused.value.code == "INSECURE_BINDING"
    assert "loadcoach token create" in refused.value.message

    created = CliRunner().invoke(cli, ["token", "create", "ops", "--scope", "admin", "--json"])
    assert created.exit_code == 0, created.output
    import json

    token = json.loads(created.stdout)["token"]
    application = bootstrap()
    with TestClient(application.app, base_url="http://coach.test") as client:
        assert client.get("/api/v1/health").status_code == 401
        assert client.get("/api/v1/health", headers=_auth(token)).status_code == 200
        assert (
            client.get("/api/v1/health", headers={"Host": "other.test", **_auth(token)}).status_code
            == 421
        )


def test_the_queue_runtime_and_cli_run_as_the_local_principal(database: Database) -> None:
    """The worker and the CLI are the operator on the machine: admin by the OS user boundary."""
    assert LOCAL.name == "local" and LOCAL.scope == "admin" and LOCAL.source == "internal"
    settings = Settings(
        provider=ProviderSettings(kind="fake"),
        execution=ExecutionSettings(max_concurrent_jobs=1),
        queue=QueueSettings(max_active_per_source=1),
    )
    sink = JobEventSink()
    first = enqueue(
        database,
        JobSubmission(task="general.chat", prompt="a", source="ideapress"),
        now=NOW,
        queue_settings=settings.queue,
        execution_settings=settings.execution,
        sink=sink,
        principal=WRITER,
    )
    assert first.created
    from loadcoach.services.queue import QueueFull

    with pytest.raises(QueueFull) as capped:
        enqueue(
            database,
            JobSubmission(task="general.chat", prompt="b", source="ideapress"),
            now=NOW,
            queue_settings=settings.queue,
            execution_settings=settings.execution,
            sink=sink,
            principal=WRITER,
        )
    assert capped.value.details == {
        "source": "ideapress",
        "active": 1,
        "max_active_per_source": 1,
    }
    other = enqueue(
        database,
        JobSubmission(task="general.chat", prompt="c", source="reviewer"),
        now=NOW,
        queue_settings=settings.queue,
        execution_settings=settings.execution,
        sink=sink,
        principal=WRITER,
    )
    assert other.created  # another source has its own cap
    with database.read() as session:
        assert session.query(Job).filter(Job.state == JobState.QUEUED.value).count() == 2
