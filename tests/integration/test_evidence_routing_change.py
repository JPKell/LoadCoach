"""Importing evidence changes routing, visibly and explicably (dev-plan P6, routing §5, §8).

Every bundle here is built by substituting into a SetSpec golden rather than by typing a document
out, so a contract change in ``capability.evidence`` breaks these tests rather than passing them.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from modelrack.testing import FakeModel, FakeProvider, FakeScript
from sweatmeter import GpuSample, TelemetrySnapshot

from loadcoach.domain.routing.subject import ProviderFacts
from loadcoach.infrastructure.db.models import Model
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.evidence import import_bundle, mark_source_unreachable
from loadcoach.services.models import discover_models
from loadcoach.services.routing import RouteRequest, RoutingPolicy, route
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

GIB = 1024**3
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
MACHINE = "9d1c4a5f2b7e83c04e6a1f9b2d5c7e83"


def _model(name: str, digest: str) -> FakeModel:
    return FakeModel(
        name=name,
        digest=digest,
        family=name.split(":")[0],
        parameter_count=8_000_000_000,
        quantization="Q8_0",
        size_bytes=8 * GIB,
        max_context=32768,
        layers=32,
        kv_heads=8,
        head_dim=128,
        declared_capabilities=frozenset(),
    )


def _database(
    tmp_path: Path, provider: FakeProvider, *, profiles_path: Path | None = None
) -> Database:
    database = Database.from_url(f"sqlite:///{tmp_path / 'route.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    if profiles_path is None:
        import_task_profiles(database, read_task_profiles_file(), now=NOW)
    else:
        import_task_profiles(database, read_task_profiles_file(profiles_path), now=NOW)
    discover_models(database, provider, now=NOW)
    return database


def _snapshot() -> TelemetrySnapshot:
    return TelemetrySnapshot(
        timestamp=NOW,
        ram_available_bytes=64 * GIB,
        gpus=(GpuSample(index=0, vram_total_bytes=48 * GIB, vram_used_bytes=1 * GIB),),
    )


def _facts() -> ProviderFacts:
    return ProviderFacts(
        healthy=True,
        context_configurable=True,
        supports_tool_use=True,
        supports_structured_output=True,
        supports_streaming=True,
    )


def _identity(database: Database, name: str) -> dict[str, Any]:
    with database.read() as session:
        model = session.query(Model).filter_by(provider_model_name=name).one()
        return {
            "provider_kind": model.provider_kind,
            "provider_model_name": model.provider_model_name,
            "artifact_digest": model.artifact_digest,
            "canonical_id": model.canonical_id,
            "identity_confidence": model.identity_confidence,
            "observed_at": "2026-08-21T09:14:02.318Z",
        }


def _record(
    template: dict[str, Any],
    *,
    identity: dict[str, Any],
    capability: str,
    score: float,
    confidence: float,
    profile_hash: str,
    machine: str = MACHINE,
    measured_at: str = "2026-08-28T09:19:41.902Z",
    computed_at: str = "2026-08-28T09:20:00.000Z",
) -> dict[str, Any]:
    """One evidence record, built from a golden so its shape is the contract's, not this file's."""
    record = copy.deepcopy(template)
    record["model"] = dict(identity)
    record["capability_id"] = capability
    record["score"] = score
    record["confidence"] = confidence
    record["runtime_profile_hash"] = profile_hash
    record["machine_fingerprint"] = machine
    record["measured_at"] = measured_at
    record["computed_at"] = computed_at
    for goal_only in (
        "goal_hash",
        "goal_pack_version",
        "score_method_mix",
        "judge_set",
        "calibration",
    ):
        record.pop(goal_only, None)
    record["judge_validity_factor"] = 1.0
    return record


def _bundle(*records: dict[str, Any], complete: bool = True) -> dict[str, Any]:
    return {"source_id": "freeweight-bench-01", "complete": complete, "evidence": list(records)}


def _route(database: Database, task: str = "general.chat", **kwargs: Any) -> dict[str, Any]:
    result = route(
        database,
        RouteRequest(task=task, estimated_input_tokens=1000),
        provider=_facts(),
        policy=RoutingPolicy(machine_fingerprint=MACHINE, **kwargs),
        snapshot=_snapshot(),
        now=NOW,
    )
    return result.explanation.payload


def _template(golden_bundle: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(golden_bundle["evidence"][0])


def _capability(payload: dict[str, Any], canonical_id: str, capability: str) -> dict[str, Any]:
    for candidate in payload["candidates"]:
        if candidate["canonical_id"] == canonical_id:
            for entry in candidate["capabilities"]:
                if entry["capability"] == capability:
                    return dict(entry)
    message = f"{capability} not found for {canonical_id}"
    raise AssertionError(message)


# --------------------------------------------------------------------------------------------
# The before/after demonstration
# --------------------------------------------------------------------------------------------


def test_importing_evidence_changes_the_selected_model_and_the_explanation_shows_why(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """dev-plan P6 Tests: the capability, score, confidence, age and source responsible."""
    provider = FakeProvider(
        FakeScript(models=(_model("alpha:8b", "a" * 64), _model("beta:8b", "b" * 64)))
    )
    database = _database(tmp_path, provider)
    try:
        before = _route(database)
        assert before["selected"]["canonical_id"].endswith("a" * 12)
        assert before["evidence_summary"]["source"] == "none"
        assert "low_evidence" in before["flags"]

        profile_hash = before["selected"]["runtime_profile_hash"]
        template = _template(golden_bundle)
        beta = _identity(database, "beta:8b")
        outcome = import_bundle(
            database,
            wrap_bundle(
                _bundle(
                    _record(
                        template,
                        identity=beta,
                        capability="reasoning",
                        score=0.94,
                        confidence=0.81,
                        profile_hash=profile_hash,
                    ),
                    _record(
                        template,
                        identity=beta,
                        capability="instruction_following",
                        score=0.88,
                        confidence=0.77,
                        profile_hash=profile_hash,
                    ),
                )
            ),
            now=NOW,
        )
        assert outcome.bound == 2

        after = _route(database)
        assert after["selected"]["canonical_id"] == beta["canonical_id"], (
            "importing evidence must change which model is selected"
        )
        assert after["evidence_summary"]["source"] == "freeweight"
        assert after["evidence_summary"]["status"] == "ok"
        assert after["evidence_summary"]["bound_records"] == 2
        assert after["evidence_summary"]["policy_version"] == "1.0.0"
        assert after["evidence_summary"]["vocabulary_version"] == "1.1"
        assert after["evidence_summary"]["bundle_schema_version"] == "1.0"
        assert after["evidence_summary"]["imported_at"] is not None
        assert after["evidence_summary"]["oldest_measured_at"] is not None
        assert "low_evidence" not in after["flags"], (
            "0.6 of the profile's weight is now measured, above the 0.5 floor"
        )

        entry = _capability(after, beta["canonical_id"], "reasoning")
        assert entry["source"] == "benchmark"
        assert entry["score"] == 0.94
        assert entry["confidence"] == 0.81
        assert entry["sample_count"] == 60
        assert entry["evidence_age_days"] == 1
        assert after["selected"]["final_score"] > before["selected"]["final_score"]
    finally:
        database.close()


def test_evidence_measured_under_another_profile_is_absent_with_both_hashes_and_a_remedy(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """ADR-0023 §3: not reused, not scored zero — absent, named, and counted as low evidence."""
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    database = _database(tmp_path, provider)
    try:
        alpha = _identity(database, "alpha:8b")
        before = _route(database)
        executing_hash = before["selected"]["runtime_profile_hash"]
        import_bundle(
            database,
            wrap_bundle(
                _bundle(
                    _record(
                        _template(golden_bundle),
                        identity=alpha,
                        capability="reasoning",
                        score=0.99,
                        confidence=0.9,
                        profile_hash="4a91deadbeefcafe",
                    )
                )
            ),
            now=NOW,
        )
        after = _route(database)
        entry = _capability(after, alpha["canonical_id"], "reasoning")
        assert entry["source"] == "evidence_profile_mismatch"
        assert entry["score"] is None, "a mismatch is absent, never zero and never reused"
        assert entry["measured_profile_hash"] == "4a91deadbeefcafe"
        assert executing_hash in entry["note"]
        assert "4a91deadbeefcafe" in entry["note"]
        assert entry["remedy"].startswith("freeweight run start")
        assert "low_evidence" in after["flags"]
        assert after["evidence_summary"]["profile_mismatched_capabilities"] == 1
    finally:
        database.close()


def test_performance_evidence_from_another_machine_is_absent_and_quality_is_badged(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """ADR-0017's machine separation: throughput describes the card it was measured on."""
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    database = _database(tmp_path, provider)
    try:
        alpha = _identity(database, "alpha:8b")
        # The profile hash must come from the task being routed: a different
        # `min_context_tokens` resolves a different served context, and therefore a different
        # profile, which would make this a mismatch test rather than a machine test.
        profile_hash = _route(database, task="general.reasoning")["selected"][
            "runtime_profile_hash"
        ]
        import_bundle(
            database,
            wrap_bundle(
                _bundle(
                    _record(
                        _template(golden_bundle),
                        identity=alpha,
                        capability="reasoning",
                        score=0.91,
                        confidence=0.8,
                        profile_hash=profile_hash,
                        machine="somewhere-else",
                    )
                )
            ),
            now=NOW,
        )
        after = _route(database, task="general.reasoning")
        reasoning = _capability(after, alpha["canonical_id"], "reasoning")
        assert reasoning["source"] == "benchmark", "quality travels between machines"
        assert reasoning["measured_machine_fingerprint"] == "somewhere-else"
        assert "not on this one" in reasoning["note"]
        assert "machine badge" in reasoning["note"]
        assert after["evidence_summary"]["foreign_machine_capabilities"] == 0
    finally:
        database.close()


def test_a_performance_capability_from_another_machine_is_excluded_by_name(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    profiles = tmp_path / "profiles.toml"
    profiles.write_text(
        '[task_profiles."speed.only"]\n'
        'version = "1.0.0"\n'
        'description = "A profile weighted on a machine-sensitive capability."\n'
        '[task_profiles."speed.only".weights]\n'
        "speed = 1.0\n"
        '[task_profiles."speed.only".constraints]\n'
        "min_context_tokens = 4096\n"
        '[task_profiles."speed.only".execution]\n'
        "max_output_tokens = 512\n"
    )
    database = _database(tmp_path, provider, profiles_path=profiles)
    try:
        alpha = _identity(database, "alpha:8b")
        profile_hash = _route(database, task="speed.only")["selected"]["runtime_profile_hash"]
        import_bundle(
            database,
            wrap_bundle(
                _bundle(
                    _record(
                        _template(golden_bundle),
                        identity=alpha,
                        capability="speed",
                        score=0.95,
                        confidence=0.9,
                        profile_hash=profile_hash,
                        machine="somewhere-else",
                    )
                )
            ),
            now=NOW,
        )
        after = _route(database, task="speed.only")
        entry = _capability(after, alpha["canonical_id"], "speed")
        assert entry["source"] == "evidence_foreign_machine"
        assert entry["score"] is None
        assert entry["measured_machine_fingerprint"] == "somewhere-else"
        assert "low_evidence" in after["flags"]
        assert after["evidence_summary"]["foreign_machine_capabilities"] == 1
    finally:
        database.close()


def test_unmatched_evidence_contributes_nothing_and_is_counted(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    database = _database(tmp_path, provider)
    try:
        before = _route(database)
        import_bundle(database, wrap_bundle(golden_bundle), now=NOW)
        after = _route(database)
        assert after["evidence_summary"]["unmatched_records"] == 3
        assert after["evidence_summary"]["bound_records"] == 0
        assert after["evidence_summary"]["source"] == "none"
        assert after["selected"]["final_score"] == before["selected"]["final_score"]
    finally:
        database.close()


# --------------------------------------------------------------------------------------------
# The user.* opt-in (ADR-0032 §6)
# --------------------------------------------------------------------------------------------


def _goal_profiles_file(tmp_path: Path, *, name: str, weights: str) -> Path:
    path = tmp_path / f"{name}.toml"
    path.write_text(
        f'[task_profiles."{name}"]\n'
        'version = "1.0.0"\n'
        'description = "House voice."\n'
        f'[task_profiles."{name}".weights]\n{weights}'
        f'[task_profiles."{name}".constraints]\n'
        "min_context_tokens = 4096\n"
        f'[task_profiles."{name}".execution]\n'
        "max_output_tokens = 512\n"
    )
    return path


def _user_bundle(
    database: Database, golden_bundle: dict[str, Any], profile_hash: str
) -> dict[str, Any]:
    goal_template = copy.deepcopy(golden_bundle["evidence"][1])
    goal_template["model"] = _identity(database, "alpha:8b")
    goal_template["runtime_profile_hash"] = profile_hash
    goal_template["machine_fingerprint"] = MACHINE
    goal_template["measured_at"] = "2026-08-28T11:02:18.640Z"
    goal_template["computed_at"] = "2026-08-28T11:05:00.000Z"
    goal_template["capability_id"] = "user.noir_tech_voice"
    goal_template["score"] = 0.97
    goal_template["confidence"] = 0.61
    return _bundle(goal_template)


def test_importing_a_user_capability_changes_no_existing_routing_outcome(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """ADR-0032 §6: a capability one person's taste defines gains no influence by existing."""
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    profiles = _goal_profiles_file(tmp_path, name="voice.plain", weights="creative_writing = 1.0\n")
    database = _database(tmp_path, provider, profiles_path=profiles)
    try:
        before = _route(database, task="voice.plain")
        profile_hash = before["selected"]["runtime_profile_hash"]
        import_bundle(
            database, wrap_bundle(_user_bundle(database, golden_bundle, profile_hash)), now=NOW
        )
        after = _route(database, task="voice.plain")
        assert after["selected"]["final_score"] == before["selected"]["final_score"]
        assert after["candidates"][0]["capabilities"] == before["candidates"][0]["capabilities"]
        assert after["flags"] == before["flags"]
        assert after["evidence_summary"]["bound_records"] == 1, (
            "the record is stored and visible; it simply does not score"
        )
        assert after["evidence_summary"]["source"] == "none"
    finally:
        database.close()


def test_naming_a_user_capability_in_the_profile_makes_it_score_with_no_re_import(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    plain = _goal_profiles_file(tmp_path, name="voice.plain", weights="creative_writing = 1.0\n")
    database = _database(tmp_path, provider, profiles_path=plain)
    try:
        profile_hash = _route(database, task="voice.plain")["selected"]["runtime_profile_hash"]
        import_bundle(
            database, wrap_bundle(_user_bundle(database, golden_bundle, profile_hash)), now=NOW
        )
        silent = _route(database, task="voice.plain")
        assert silent["evidence_summary"]["source"] == "none"

        naming = _goal_profiles_file(
            tmp_path,
            name="voice.house",
            weights='creative_writing = 0.5\n"user.noir_tech_voice" = 0.5\n',
        )
        import_task_profiles(database, read_task_profiles_file(naming), now=NOW)

        scored = _route(database, task="voice.house")
        entry = _capability(scored, scored["selected"]["canonical_id"], "user.noir_tech_voice")
        assert entry["source"] == "benchmark"
        assert entry["score"] == 0.97
        assert scored["evidence_summary"]["source"] == "freeweight"
    finally:
        database.close()


def test_an_explanation_that_used_a_user_capability_states_the_goal_kappa_and_holdout_in_words(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """ADR-0032 §6: in the rendered text, not only in the machine-readable breakdown."""
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    naming = _goal_profiles_file(
        tmp_path,
        name="voice.house",
        weights='creative_writing = 0.5\n"user.noir_tech_voice" = 0.5\n',
    )
    database = _database(tmp_path, provider, profiles_path=naming)
    try:
        profile_hash = _route(database, task="voice.house")["selected"]["runtime_profile_hash"]
        import_bundle(
            database, wrap_bundle(_user_bundle(database, golden_bundle, profile_hash)), now=NOW
        )
        payload = _route(database, task="voice.house")
        entry = _capability(payload, payload["selected"]["canonical_id"], "user.noir_tech_voice")
        note = entry["note"]
        assert "noir_tech_voice" in note
        assert "kappa_w 0.74" in note
        assert "18 held-out samples" in note
        assert "the goal's author" in note
        assert "2026-08-20" in note
    finally:
        database.close()


# --------------------------------------------------------------------------------------------
# Degradation: FreeWeight absent or unreachable
# --------------------------------------------------------------------------------------------


def test_with_no_evidence_source_configured_the_explanation_says_so(tmp_path: Path) -> None:
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    database = _database(tmp_path, provider)
    try:
        payload = _route(database)
        summary = payload["evidence_summary"]
        assert summary["status"] == "not_configured"
        assert "no evidence source is configured" in summary["note"].lower()
        assert "priors" in summary["note"]
        assert payload["selected"] is not None, "routing still works with no evidence at all"
    finally:
        database.close()


def test_an_unreachable_freeweight_keeps_routing_on_the_last_import_and_says_so(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """P6 acceptance criterion 3, end to end through the explanation."""
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    database = _database(tmp_path, provider)
    try:
        alpha = _identity(database, "alpha:8b")
        profile_hash = _route(database)["selected"]["runtime_profile_hash"]
        import_bundle(
            database,
            wrap_bundle(
                _bundle(
                    _record(
                        _template(golden_bundle),
                        identity=alpha,
                        capability="reasoning",
                        score=0.9,
                        confidence=0.8,
                        profile_hash=profile_hash,
                    )
                )
            ),
            now=NOW,
            source_kind="freeweight_api",
            url="http://127.0.0.1:8765",
        )
        healthy = _route(database, evidence_url="http://127.0.0.1:8765")
        assert healthy["evidence_summary"]["status"] == "ok"

        mark_source_unreachable(
            database, url="http://127.0.0.1:8765", reason="connection refused", now=NOW
        )
        degraded = _route(database, evidence_url="http://127.0.0.1:8765")
        summary = degraded["evidence_summary"]
        assert summary["status"] == "unreachable"
        assert "could not be reached" in summary["note"]
        assert "retained" in summary["note"]
        assert summary["stale"] is True
        assert degraded["selected"] is not None
        entry = _capability(degraded, alpha["canonical_id"], "reasoning")
        assert entry["source"] == "benchmark", "the retained evidence is still used"
        assert entry["stale"] is True
        assert entry["stale_reason"] == "source_unreachable"
    finally:
        database.close()


def test_freshness_uses_measured_at_so_re_aggregation_does_not_change_a_decision(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    database = _database(tmp_path, provider)
    try:
        alpha = _identity(database, "alpha:8b")
        profile_hash = _route(database)["selected"]["runtime_profile_hash"]
        template = _template(golden_bundle)
        old_measurement = _record(
            template,
            identity=alpha,
            capability="reasoning",
            score=0.9,
            confidence=0.8,
            profile_hash=profile_hash,
            measured_at="2026-01-01T00:00:00.000Z",
            computed_at="2026-01-02T00:00:00.000Z",
        )
        import_bundle(database, wrap_bundle(_bundle(old_measurement)), now=NOW)
        first = _capability(_route(database), alpha["canonical_id"], "reasoning")

        re_aggregated = copy.deepcopy(old_measurement)
        re_aggregated["computed_at"] = "2026-08-29T00:00:00.000Z"
        import_bundle(database, wrap_bundle(_bundle(re_aggregated)), now=NOW)
        second = _capability(_route(database), alpha["canonical_id"], "reasoning")

        assert second["evidence_age_days"] == first["evidence_age_days"]
        assert second["confidence"] == first["confidence"]
        assert second["stale_reason"] == "freshness"
    finally:
        database.close()


@pytest.mark.parametrize("age_days", [1, 400])
def test_confidence_is_applied_never_recomputed(
    tmp_path: Path,
    golden_bundle: dict[str, Any],
    wrap_bundle: Callable[..., str],
    age_days: int,
) -> None:
    """ADR-0017: FreeWeight computes confidence, LoadCoach applies it — at any age."""
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    database = _database(tmp_path, provider)
    try:
        alpha = _identity(database, "alpha:8b")
        profile_hash = _route(database)["selected"]["runtime_profile_hash"]
        measured = NOW - timedelta(days=age_days)
        import_bundle(
            database,
            wrap_bundle(
                _bundle(
                    _record(
                        _template(golden_bundle),
                        identity=alpha,
                        capability="reasoning",
                        score=0.9,
                        confidence=0.8,
                        profile_hash=profile_hash,
                        measured_at=measured.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        computed_at=measured.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    )
                )
            ),
            now=NOW,
        )
        entry = _capability(_route(database), alpha["canonical_id"], "reasoning")
        assert entry["confidence"] == 0.8, "the stored number, not a locally decayed one"
        assert entry["evidence_age_days"] == age_days
    finally:
        database.close()
