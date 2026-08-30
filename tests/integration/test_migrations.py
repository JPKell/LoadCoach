"""LoadCoach's own migration history, and the WeightsDB two-schemas proof (dev-plan P1 Tests).

Head is read from the script directory rather than written down, so adding a revision (P3's
``0002``, and every phase after it) does not require editing an assertion that was never about
the revision number in the first place.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import text
from weightsdb import MigrationRunner
from weightsdb.errors import MigrationFailed
from weightsdb.testing import temporary_postgres, temporary_sqlite

from loadcoach.infrastructure.db.models import Base
from loadcoach.services.database import MIGRATIONS_LOCATION

_OTHER_APP_SCRIPT_LOCATION = str(Path(__file__).parent / "_other_app_fixture")
_OTHER_APP_TABLE = "machines"

_BROKEN_REVISION_ID = "9999"


def _broken_revision(down_revision: str) -> str:
    """A revision that always fails, stacked on whatever the real head currently is."""
    return (
        f'"""broken\n\nRevision ID: {_BROKEN_REVISION_ID}\nRevises: {down_revision}\n"""\n'
        "from __future__ import annotations\n\n"
        f'revision: str = "{_BROKEN_REVISION_ID}"\n'
        f'down_revision: str | None = "{down_revision}"\n'
        "branch_labels = None\n"
        "depends_on = None\n\n"
        "def upgrade() -> None:\n"
        "    raise RuntimeError('deliberate failure')\n\n"
        "def downgrade() -> None:\n"
        "    pass\n"
    )


def _head() -> str:
    """The single head of LoadCoach's own linear history."""
    with temporary_sqlite() as engine:
        heads = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).heads()
    assert len(heads) == 1, f"LoadCoach's history must stay linear; found heads {heads}"
    return heads[0]


def test_fresh_database_migrates_to_head_sqlite() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        assert runner.current() is None
        outcome = runner.upgrade(backup=False)
        assert outcome.to_revision == _head()
        assert runner.is_at_head()


def test_fresh_database_migrates_to_head_postgres() -> None:
    with temporary_postgres() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        outcome = runner.upgrade(backup=False)
        assert outcome.to_revision == _head()
        assert runner.is_at_head()


def test_upgrade_head_twice_is_idempotent() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        second = runner.upgrade(backup=False)
        assert second.backed_up is False
        assert second.from_revision == second.to_revision == _head()


def test_check_parity_matches_models_after_upgrade() -> None:
    """models.py and the migration history describe the same schema (database standards §5.2)."""
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        result = runner.check_parity(Base.metadata)
        assert result.matches, result.diff


def test_check_parity_matches_on_postgresql() -> None:
    with temporary_postgres() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        result = runner.check_parity(Base.metadata)
        assert result.matches, result.diff


def test_two_independent_migration_histories_share_one_database_no_collision() -> None:
    """Gold standard: shared plumbing, zero shared schema, proved side by side.

    A stand-in for a second application's own schema (see ``_other_app_fixture``'s module
    docstring for why it is a stand-in rather than the real FreeWeight package) migrates through
    the *same* WeightsDB plumbing, against the *same* physical database file as LoadCoach's own
    migration, using a distinct ``version_table`` — exercising the specific failure mode named for
    this phase: version-table naming collisions between the two schemas.
    """
    with temporary_sqlite() as engine:
        loadcoach_runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        loadcoach_runner.upgrade(backup=False)

        other_runner = MigrationRunner(
            engine,
            script_location=_OTHER_APP_SCRIPT_LOCATION,
            version_table="alembic_version_other_app",
        )
        other_runner.upgrade(backup=False)

        assert loadcoach_runner.is_at_head()
        assert other_runner.is_at_head()

        loadcoach_tables = set(Base.metadata.tables)
        assert _OTHER_APP_TABLE not in loadcoach_tables

        with engine.connect() as connection:
            existing = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            }
        assert loadcoach_tables <= existing
        assert _OTHER_APP_TABLE in existing
        assert "alembic_version" in existing
        assert "alembic_version_other_app" in existing

        # check_parity is not run here: it diffs the *entire* live schema against the given
        # metadata, and this test deliberately shares one physical file between two schemas —
        # something database standards §1 forbids in any real deployment ("one database per
        # application"). The properties that matter — both histories reach their own head under
        # their own version_table with no collision, and neither's tables appear in the other's
        # metadata — are asserted above.


def test_failed_migration_restores_backup_on_sqlite(tmp_path: Path) -> None:
    """A deliberately failing revision on top of head: backup restored, both outcomes reported."""
    head = _head()
    broken_dir = tmp_path / "broken_migrations"
    shutil.copytree(MIGRATIONS_LOCATION, broken_dir)
    (broken_dir / "versions" / "9999_broken.py").write_text(_broken_revision(head))

    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=str(broken_dir))
        runner.upgrade(head, backup=False)

        with engine.connect() as connection:
            connection.execute(
                text("INSERT INTO settings (key, updated_at) VALUES ('k', :updated_at)"),
                {"updated_at": "2026-08-29T00:00:00"},
            )
            connection.commit()

        with pytest.raises(MigrationFailed) as excinfo:
            runner.upgrade(_BROKEN_REVISION_ID)
        assert excinfo.value.details["restored"] is True
        assert runner.current() == head

        with engine.connect() as connection:
            rows = connection.execute(text("SELECT key FROM settings")).fetchall()
        assert [row[0] for row in rows] == ["k"]


def test_migration_0004_adds_residency_and_fixes_the_claim_index_direction() -> None:
    """P5's migration: the ``residency`` table (data model §2) and the claim index's ``DESC``.

    ``0003`` created ``(state, effective_priority, created_at)`` ascending; the data model names
    ``(state, effective_priority DESC, created_at)``. The direction is what lets the claim's
    ``ORDER BY effective_priority DESC, created_at ASC`` walk the index — without it SQLite sorts
    every equal-priority job through a temp B-tree on every claim, and ``EXPLAIN QUERY PLAN``
    is the witness either way.
    """
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        with engine.connect() as connection:
            names = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table'")
                )
            }
            assert "residency" in names
            columns = {
                row[1]
                for row in connection.execute(text("PRAGMA table_info(residency)")).fetchall()
            }
            assert columns == {
                "id",
                "model_id",
                "gpu_index",
                "loaded_at",
                "last_used_at",
                "vram_bytes",
                "vram_bytes_unavailable_reason",
                "resident",
                "unloaded_at",
                "unload_reason",
            }
            index_sql = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE name = 'ix_jobs_state_effective_priority_created_at'"
                )
            ).scalar_one()
            assert "effective_priority DESC" in index_sql
            plan = [
                row[3]
                for row in connection.execute(
                    text(
                        "EXPLAIN QUERY PLAN SELECT id FROM jobs WHERE state = 'queued' "
                        "AND scheduled_for <= '2026-01-01 00:00:00' "
                        "ORDER BY effective_priority DESC, created_at ASC LIMIT 1"
                    )
                ).fetchall()
            ]
            assert any("ix_jobs_state_effective_priority_created_at" in step for step in plan)
            assert not any("TEMP B-TREE" in step for step in plan), plan
            assert not any(step.startswith("SCAN") for step in plan), plan


def _seed_evidence(engine: object) -> None:
    """Fill ``capability_evidence`` with a realistic shape and ANALYZE it.

    Twenty models, ten capabilities, one profile: ``match_state`` is a near-constant column and
    ``(model_id, capability_id)`` is highly selective, which is the distribution data model §4's
    requirement is written for. Without statistics SQLite cannot know that.
    """
    with engine.begin() as connection:  # type: ignore[attr-defined]  # Engine, kept untyped here
        connection.execute(
            text(
                "INSERT INTO evidence_sources (id, source_key, kind, record_count, created_at) "
                "VALUES ('S0000000000000000000000001', 'fw', 'freeweight_api', 0, "
                "'2026-08-29 00:00:00')"
            )
        )
        for model_index in range(20):
            model_id = f"M{model_index:025d}"
            connection.execute(
                text(
                    "INSERT INTO models (id, provider_kind, provider_model_name, canonical_id, "
                    "identity_confidence, first_seen_at, last_seen_at, available) VALUES "
                    "(:id, 'ollama', :name, :canonical, 'digest', '2026-08-01 00:00:00', "
                    "'2026-08-01 00:00:00', 1)"
                ),
                {
                    "id": model_id,
                    "name": f"model-{model_index}",
                    "canonical": f"ollama/model-{model_index}@sha256:{model_index:012d}",
                },
            )
            for capability in (
                "reasoning",
                "coding",
                "code_review",
                "auditing",
                "debugging",
                "instruction_following",
                "structured_output",
                "tool_use",
                "long_context",
                "speed",
            ):
                connection.execute(
                    text(
                        "INSERT INTO capability_evidence (id, model_id, provider_kind, "
                        "provider_model_name, canonical_id, match_state, runtime_profile_hash, "
                        "machine_fingerprint, capability_id, score, confidence, sample_count, "
                        "excluded_count, identity_confidence, measured_at, computed_at, "
                        "imported_at, source_id, policy_version, vocabulary_version, stale) "
                        "VALUES (:id, :model_id, 'ollama', :name, :canonical, 'bound', '8f2c', "
                        "'here', :capability, 0.7, 0.6, 40, 0, 'digest', "
                        "'2026-08-01 00:00:00', '2026-08-01 00:00:00', '2026-08-29 00:00:00', "
                        "'S0000000000000000000000001', '1.0.0', '1.1', 0)"
                    ),
                    {
                        "id": f"E{model_index:012d}{capability[:12]:_>12}"[:26],
                        "model_id": model_id,
                        "name": f"model-{model_index}",
                        "canonical": f"ollama/model-{model_index}@sha256:{model_index:012d}",
                        "capability": capability,
                    },
                )
        connection.execute(text("ANALYZE"))


def test_migration_0005_adds_the_two_evidence_tables_and_nothing_else() -> None:
    """P6's migration: ``capability_evidence`` and ``evidence_sources`` (data model §2).

    "And nothing else" is the assertion that matters — re-declaring a column an earlier migration
    created is drift, and ``check_parity`` is what reports it. The evidence lookup's query plan is
    asserted rather than assumed, the same way the claim query's was in ``0004``: data model §4
    requires it to use ``(model_id, capability_id)`` and to filter on ``match_state = 'bound'``.
    """
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        assert runner.check_parity(Base.metadata).matches

        with engine.connect() as connection:
            names = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table'")
                )
            }
            assert {"capability_evidence", "evidence_sources"} <= names

            columns = {
                row[1]
                for row in connection.execute(
                    text("PRAGMA table_info(capability_evidence)")
                ).fetchall()
            }
            assert columns == {
                "id",
                "model_id",
                "provider_kind",
                "provider_model_name",
                "artifact_digest",
                "canonical_id",
                "match_state",
                "runtime_profile_hash",
                "machine_fingerprint",
                "capability_id",
                "score",
                "confidence",
                "sample_count",
                "excluded_count",
                "dispersion",
                "dispersion_unavailable_reason",
                "benchmark_versions_json",
                "dataset_hashes_json",
                "prompt_subset_hashes_json",
                "contributing_metrics_json",
                "source_run_ids_json",
                "identity_confidence",
                "environment_snapshot_json",
                "goal_json",
                "measured_at",
                "computed_at",
                "imported_at",
                "source_id",
                "policy_version",
                "vocabulary_version",
                "stale",
                "stale_reason",
                "record_json",
            }
            source_columns = {
                row[1]
                for row in connection.execute(
                    text("PRAGMA table_info(evidence_sources)")
                ).fetchall()
            }
            assert source_columns == {
                "id",
                "source_key",
                "kind",
                "url",
                "last_import_at",
                "last_status",
                "schema_version",
                "record_count",
                "error_text",
                "generated_at",
                "created_at",
            }

        # The planner needs statistics to choose between the three indexes: on an empty table it
        # prefers whichever it meets first, which says nothing about how the query behaves in an
        # installation with evidence in it. Populate a realistic shape, ANALYZE, then assert.
        _seed_evidence(engine)
        with engine.connect() as connection:
            plan = [
                row[3]
                for row in connection.execute(
                    text(
                        "EXPLAIN QUERY PLAN SELECT score FROM capability_evidence "
                        "WHERE model_id = 'M0000000000000000000000005' "
                        "AND capability_id = 'reasoning' AND match_state = 'bound'"
                    )
                ).fetchall()
            ]
            assert any("ix_capability_evidence_model_id_capability_id" in step for step in plan), (
                plan
            )
            assert not any(step.startswith("SCAN") for step in plan), plan

            unbound_plan = [
                row[3]
                for row in connection.execute(
                    text(
                        "EXPLAIN QUERY PLAN SELECT score FROM capability_evidence "
                        "WHERE canonical_id = 'ollama/x@sha256:1' AND capability_id = 'reasoning'"
                    )
                ).fetchall()
            ]
            assert any(
                "ix_capability_evidence_canonical_id_capability_id" in step for step in unbound_plan
            ), unbound_plan
            assert not any(step.startswith("SCAN") for step in unbound_plan), unbound_plan


def test_migration_0005_touches_no_column_an_earlier_revision_created() -> None:
    """Drift check: the schema at ``0004`` and the schema at ``0005`` differ by two tables only."""
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(revision="0004", backup=False)
        with engine.connect() as connection:
            before = {
                row[0]: row[1]
                for row in connection.execute(
                    text("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
                )
            }
        runner.upgrade(revision="0005", backup=False)
        with engine.connect() as connection:
            after = {
                row[0]: row[1]
                for row in connection.execute(
                    text("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
                )
            }
    assert set(after) - set(before) == {"capability_evidence", "evidence_sources"}
    for name, sql in before.items():
        assert after[name] == sql, name


def test_migration_0006_adds_feedback_and_reliability_stats_and_nothing_else() -> None:
    """P7's migration: ``feedback`` and ``reliability_stats`` (data model §2).

    The reliability lookup's query plan is asserted the same way the evidence lookup's was in
    ``0005``: data model §4 requires it to use ``(model_id, task_profile_id, window)``, which is
    the uniqueness key — a point lookup, never a scan.
    """
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        assert runner.check_parity(Base.metadata).matches

        with engine.connect() as connection:
            names = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table'")
                )
            }
            assert {"feedback", "reliability_stats"} <= names
            feedback_columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(feedback)")).fetchall()
            }
            assert feedback_columns == {
                "id",
                "job_id",
                "source",
                "accepted",
                "quality_score",
                "edited",
                "validation_passed",
                "validation_detail_json",
                "notes",
                "created_at",
                "updated_at",
            }
            stats_columns = {
                row[1]
                for row in connection.execute(
                    text("PRAGMA table_info(reliability_stats)")
                ).fetchall()
            }
            assert stats_columns == {
                "id",
                "model_id",
                "task_profile_id",
                "window",
                "attempts",
                "successes",
                "validation_passes",
                "errors",
                "timeouts",
                "cancellations",
                "latency_count",
                "p50_latency_ms",
                "p95_latency_ms",
                "output_token_count",
                "mean_output_tokens",
                "tokens_per_second_count",
                "mean_tokens_per_second",
                "feedback_count",
                "acceptance_rate",
                "quality_count",
                "mean_quality",
                "circuit_state",
                "circuit_opened_at",
                "circuit_reason",
                "updated_at",
            }

        with engine.begin() as connection:
            for model_index in range(20):
                model_id = f"M{model_index:025d}"
                connection.execute(
                    text(
                        "INSERT INTO models (id, provider_kind, provider_model_name, canonical_id, "
                        "identity_confidence, first_seen_at, last_seen_at, available) VALUES "
                        "(:id, 'ollama', :name, :canonical, 'digest', '2026-08-01 00:00:00', "
                        "'2026-08-01 00:00:00', 1)"
                    ),
                    {
                        "id": model_id,
                        "name": f"model-{model_index}",
                        "canonical": f"ollama/model-{model_index}@sha256:{model_index:012d}",
                    },
                )
                for profile in ("general.chat", "code.review", "data.extract"):
                    for window in ("7d", "30d", "all"):
                        connection.execute(
                            text(
                                "INSERT INTO reliability_stats (id, model_id, task_profile_id, "
                                "window, attempts, successes, validation_passes, errors, "
                                "timeouts, cancellations, latency_count, output_token_count, "
                                "tokens_per_second_count, feedback_count, quality_count, "
                                "circuit_state, updated_at) VALUES (:id, :model_id, :profile, "
                                ":window, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 'closed', "
                                "'2026-08-30 00:00:00')"
                            ),
                            {
                                "id": f"R{model_index:03d}{profile[:10]:<10}{window:>12}".replace(
                                    " ", "0"
                                )[:26].ljust(26, "0"),
                                "model_id": model_id,
                                "profile": profile,
                                "window": window,
                            },
                        )
            connection.execute(text("ANALYZE"))
        with engine.connect() as connection:
            plan = [
                row[3]
                for row in connection.execute(
                    text(
                        "EXPLAIN QUERY PLAN SELECT attempts FROM reliability_stats "
                        "WHERE model_id = 'M0000000000000000000000005' "
                        "AND task_profile_id = 'code.review' AND window = '7d'"
                    )
                ).fetchall()
            ]
            assert any("model_id=? AND task_profile_id=? AND window=?" in step for step in plan), (
                plan
            )
            assert not any(step.startswith("SCAN") for step in plan), plan


def test_migration_0006_touches_no_column_an_earlier_revision_created() -> None:
    """Drift check: the schema at ``0005`` and the schema at ``0006`` differ by two tables only."""
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(revision="0005", backup=False)
        with engine.connect() as connection:
            before = {
                row[0]: row[1]
                for row in connection.execute(
                    text("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
                )
            }
        runner.upgrade(backup=False)
        with engine.connect() as connection:
            after = {
                row[0]: row[1]
                for row in connection.execute(
                    text("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
                )
            }
    assert set(after) - set(before) == {"feedback", "reliability_stats"}
    for name, sql in before.items():
        assert after[name] == sql, name
