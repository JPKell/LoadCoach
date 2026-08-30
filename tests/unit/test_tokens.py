"""``loadcoach token create|list|revoke`` and the service behind it (spec §7.2, api.md §11)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from baseaicore import ValidationError
from typer.testing import CliRunner

from loadcoach.cli.main import app
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.tokens import TokenNotFound, create_token, list_tokens, revoke_token
from loadcoach.web.auth import Forbidden, Unauthorized, require_scope, token_sha256

runner = CliRunner()
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    url = f"sqlite:///{tmp_path / 'tokens.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    handle = Database.from_url(url)
    ensure_ready(handle, auto_migrate=True)
    return handle


def test_create_stores_only_the_digest_and_the_token_authorizes_its_scope(
    database: Database,
) -> None:
    issued = create_token(database, name="ideapress", scope="write", expires_days=None, now=NOW)
    assert issued.record.name == "ideapress" and issued.record.scope == "write"
    assert len(issued.token) >= 40 and issued.token not in json.dumps(issued.record.as_json())
    records = list_tokens(database)
    assert [r.name for r in records] == ["ideapress"] and records[0].active
    # Loopback is no longer open once a token exists: a scoped call needs the bearer.
    with pytest.raises(Unauthorized):
        require_scope(database, required="read", authorization=None, bind_host="127.0.0.1", now=NOW)
    assert (
        require_scope(
            database,
            required="write",
            authorization=f"Bearer {issued.token}",
            bind_host="127.0.0.1",
            now=NOW,
        )
        == "ideapress"
    )
    with pytest.raises(Forbidden):
        require_scope(
            database,
            required="admin",
            authorization=f"Bearer {issued.token}",
            bind_host="127.0.0.1",
            now=NOW,
        )


def test_revoke_makes_the_token_unknown_and_frees_the_name(database: Database) -> None:
    issued = create_token(database, name="ops", scope="admin", expires_days=30, now=NOW)
    assert issued.record.expires_at == NOW + timedelta(days=30)
    with pytest.raises(ValidationError, match="already exists"):
        create_token(database, name="ops", scope="read", expires_days=None, now=NOW)
    revoked = revoke_token(database, name="ops", now=NOW + timedelta(hours=1))
    assert revoked.revoked_at == NOW + timedelta(hours=1)
    # With no active token left, loopback is open again (api.md §11) — so keep one, and the
    # revoked token must then be refused as unknown rather than admitted.
    create_token(database, name="other", scope="read", expires_days=None, now=NOW)
    with pytest.raises(Unauthorized):
        require_scope(
            database,
            required="read",
            authorization=f"Bearer {issued.token}",
            bind_host="127.0.0.1",
            now=NOW + timedelta(hours=2),
        )
    with pytest.raises(TokenNotFound):
        revoke_token(database, name="ops", now=NOW)
    again = create_token(database, name="ops", scope="read", expires_days=None, now=NOW)
    assert again.record.token_id != issued.record.token_id
    assert token_sha256(again.token) != token_sha256(issued.token)


def test_create_refuses_a_blank_name_or_an_unknown_scope(database: Database) -> None:
    with pytest.raises(ValidationError, match="name"):
        create_token(database, name="   ", scope="read", expires_days=None, now=NOW)
    with pytest.raises(ValidationError, match="Scope"):
        create_token(database, name="x", scope="root", expires_days=None, now=NOW)


def test_the_cli_prints_the_token_once_and_never_again(database: Database) -> None:
    created = runner.invoke(app, ["token", "create", "reviewer", "--scope", "read", "--json"])
    assert created.exit_code == 0, created.output
    record = json.loads(created.stdout)
    assert record["name"] == "reviewer" and record["scope"] == "read"
    secret = record["token"]
    listed = runner.invoke(app, ["token", "list"])
    assert listed.exit_code == 0 and "reviewer\tread\tactive" in listed.stdout
    assert secret not in listed.stdout
    assert secret not in runner.invoke(app, ["token", "list", "--json"]).stdout
    plain = runner.invoke(app, ["token", "create", "second", "--scope", "write"])
    assert plain.exit_code == 0 and "Shown once" in plain.stdout
    bad_scope = runner.invoke(app, ["token", "create", "third", "--scope", "root"])
    assert bad_scope.exit_code == 2
    duplicate = runner.invoke(app, ["token", "create", "reviewer"])
    assert duplicate.exit_code == 2
    revoked = runner.invoke(app, ["token", "revoke", "reviewer"])
    assert revoked.exit_code == 0 and "revoked" in revoked.stdout
    assert runner.invoke(app, ["token", "revoke", "reviewer"]).exit_code == 5
    assert "revoked" in runner.invoke(app, ["token", "list"]).stdout
    empty = runner.invoke(app, ["token", "list", "--json"])
    assert len(json.loads(empty.stdout)["tokens"]) == 2
