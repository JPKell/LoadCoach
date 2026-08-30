"""The pure rules imported evidence obeys (ADR-0022 §4, ADR-0023 §3, ADR-0017, ADR-0032 §6).

Table-driven and database-free by design: the binding rules are the part of Phase 6 a careful
implementer gets wrong, and none of them needs an importer, an HTTP client or a FreeWeight to be
proven.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from loadcoach.domain.evidence_policy import (
    FRESHNESS_FLOOR,
    PERFORMANCE_HALF_LIFE_DAYS,
    QUALITY_HALF_LIFE_DAYS,
    Binding,
    CalibrationFacts,
    EvidenceCandidate,
    EvidenceIdentity,
    LocalModel,
    bind_identity,
    capability_half_life_days,
    collapse_evidence,
    environment_drift,
    evaluate_staleness,
    freshness_factor,
    is_performance_capability,
    is_user_capability,
    known_capability,
    machine_admits,
    policy_version_key,
    profile_admits,
    user_capability_note,
    user_goal_slug,
    weights_admit,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

DIGEST = "sha256:3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f"
OTHER_DIGEST = "sha256:a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"


def _identity(*, digest: str | None = DIGEST, name: str = "qwen3.5:32b") -> EvidenceIdentity:
    return EvidenceIdentity(
        provider_kind="ollama",
        provider_model_name=name,
        artifact_digest=digest,
        canonical_id=f"ollama/{name}@{digest or 'unknown'}",
    )


def _local(
    *, digest: str | None = DIGEST, name: str = "qwen3.5:32b", model_id: str = "M1"
) -> LocalModel:
    return LocalModel(
        model_id=model_id,
        provider_kind="ollama",
        provider_model_name=name,
        artifact_digest=digest,
        canonical_id=f"ollama/{name}@{digest or 'unknown'}",
    )


# --------------------------------------------------------------------------------------------
# ADR-0022 §4 — the binding table, in both directions
# --------------------------------------------------------------------------------------------


def test_an_exact_triple_match_binds() -> None:
    binding = bind_identity(_identity(), [_local()])
    assert binding == Binding(
        match_state="bound",
        model_id="M1",
        upgrade_model_id=None,
        upgrade_digest=None,
        note=binding.note,
    )
    assert binding.is_bound


def test_two_name_only_identities_are_an_exact_triple_match() -> None:
    """Both digests are ``None``, so the triple matches: this is rule 1, not rule 3."""
    binding = bind_identity(_identity(digest=None), [_local(digest=None)])
    assert binding.match_state == "bound"
    assert binding.model_id == "M1"


def test_a_digest_in_the_bundle_upgrades_a_local_name_only_row() -> None:
    binding = bind_identity(_identity(digest=DIGEST), [_local(digest=None)])
    assert binding.match_state == "bound"
    assert binding.model_id == "M1"
    assert binding.upgrade_model_id == "M1"
    assert binding.upgrade_digest == DIGEST
    assert "upgraded" in binding.note


def test_name_only_evidence_against_a_digested_row_stays_ambiguous_and_never_binds() -> None:
    """Rule 3 — the one that gets lost. It is not 'scores with low confidence'."""
    binding = bind_identity(_identity(digest=None), [_local(digest=DIGEST)])
    assert binding.match_state == "ambiguous_name_only"
    assert binding.model_id is None
    assert binding.upgrade_model_id is None
    assert not binding.is_bound


def test_evidence_for_a_model_nobody_has_discovered_is_unmatched_not_rejected() -> None:
    binding = bind_identity(_identity(), [])
    assert binding.match_state == "unmatched"
    assert binding.model_id is None
    assert "bound automatically" in binding.note


def test_a_different_digest_for_the_same_name_is_unmatched_not_bound() -> None:
    """Different weights under one name are a different subject, not a near miss."""
    binding = bind_identity(_identity(digest=DIGEST), [_local(digest=OTHER_DIGEST)])
    assert binding.match_state == "unmatched"
    assert binding.model_id is None


def test_the_exact_match_wins_over_the_name_only_upgrade_when_both_rows_exist() -> None:
    registry = [_local(digest=None, model_id="NAME"), _local(digest=DIGEST, model_id="EXACT")]
    binding = bind_identity(_identity(digest=DIGEST), registry)
    assert binding.model_id == "EXACT"
    assert binding.upgrade_model_id is None


def test_binding_ignores_a_model_of_another_provider_kind_or_name() -> None:
    other_kind = LocalModel(
        model_id="X",
        provider_kind="llamacpp",
        provider_model_name="qwen3.5:32b",
        artifact_digest=DIGEST,
        canonical_id="llamacpp/qwen3.5:32b@" + DIGEST,
    )
    assert bind_identity(_identity(), [other_kind]).match_state == "unmatched"
    assert bind_identity(_identity(), [_local(name="gemma4:12b")]).match_state == "unmatched"


def test_binding_does_not_depend_on_registry_order() -> None:
    registry = [_local(digest=None, model_id="NAME"), _local(digest=DIGEST, model_id="EXACT")]
    forward = bind_identity(_identity(digest=DIGEST), registry)
    backward = bind_identity(_identity(digest=DIGEST), list(reversed(registry)))
    assert forward == backward


# --------------------------------------------------------------------------------------------
# ADR-0032 §6 — the user.* opt-in
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("capability_id", "expected"),
    [
        ("user.noir_tech_voice", True),
        ("user.house_voice", True),
        ("user", False),
        ("user.", False),
        ("reasoning", False),
        ("coding.python", False),
    ],
)
def test_is_user_capability_covers_the_bare_root_and_the_empty_slug(
    capability_id: str, expected: bool
) -> None:
    assert is_user_capability(capability_id) is expected


def test_user_goal_slug_is_everything_after_the_first_dot() -> None:
    assert user_goal_slug("user.noir_tech_voice") == "noir_tech_voice"
    assert user_goal_slug("reasoning") is None


def test_a_user_capability_the_profile_does_not_name_is_not_admitted() -> None:
    assert weights_admit("user.house_voice", {"reasoning": 1.0}) is False


def test_a_user_capability_the_profile_names_is_admitted() -> None:
    assert weights_admit("user.house_voice", {"user.house_voice": 0.4}) is True


def test_every_non_user_capability_is_admitted_regardless_of_the_weights() -> None:
    assert weights_admit("reasoning", {}) is True
    assert weights_admit("coding.python", {"reasoning": 1.0}) is True


def test_the_user_note_states_the_goal_kappa_and_holdout_in_words() -> None:
    calibration = CalibrationFacts(
        kappa_w=0.74,
        n_holdout=18,
        graded_by="the goal's author",
        measured_at=datetime(2026, 8, 20, 17, 2, 11, tzinfo=UTC),
    )
    note = user_capability_note("user.noir_tech_voice", calibration)
    assert "noir_tech_voice" in note
    assert "kappa_w 0.74" in note
    assert "18 held-out samples" in note
    assert "the goal's author" in note
    assert "2026-08-20" in note


def test_a_rules_only_goal_says_no_judge_agreement_was_measured() -> None:
    note = user_capability_note("user.rules_only", None)
    assert "rules_only" in note
    assert "no judge agreement" in note


# --------------------------------------------------------------------------------------------
# ADR-0017 — freshness, half-lives and staleness
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("capability_id", "expected"),
    [
        ("speed", PERFORMANCE_HALF_LIFE_DAYS),
        ("latency.ttft", PERFORMANCE_HALF_LIFE_DAYS),
        ("memory_efficiency", PERFORMANCE_HALF_LIFE_DAYS),
        ("energy_efficiency", PERFORMANCE_HALF_LIFE_DAYS),
        ("reasoning", QUALITY_HALF_LIFE_DAYS),
        ("coding.python", QUALITY_HALF_LIFE_DAYS),
        ("token_efficiency", QUALITY_HALF_LIFE_DAYS),
        ("user.house_voice", QUALITY_HALF_LIFE_DAYS),
    ],
)
def test_half_lives_follow_the_capability_root(capability_id: str, expected: int) -> None:
    assert capability_half_life_days(capability_id) == expected
    assert is_performance_capability(capability_id) is (expected == PERFORMANCE_HALF_LIFE_DAYS)


def test_freshness_halves_at_one_half_life_and_floors_at_the_documented_value() -> None:
    measured = NOW - timedelta(days=QUALITY_HALF_LIFE_DAYS)
    assert freshness_factor(measured, NOW, "reasoning") == pytest.approx(0.5)
    ancient = NOW - timedelta(days=10 * QUALITY_HALF_LIFE_DAYS)
    assert freshness_factor(ancient, NOW, "reasoning") == FRESHNESS_FLOOR


def test_freshness_uses_measured_at_only_and_a_future_measurement_is_merely_new() -> None:
    assert freshness_factor(NOW + timedelta(days=5), NOW, "reasoning") == 1.0


def test_recomputing_evidence_does_not_make_it_fresher() -> None:
    """ADR-0022 §2, stated as an experiment: only ``measured_at`` moves the number.

    ``freshness_factor`` has no parameter for ``computed_at``, so the two calls below differ in
    nothing a re-aggregation could change — which is exactly the point.
    """
    measured = NOW - timedelta(days=120)
    before = freshness_factor(measured, NOW, "reasoning")
    after = freshness_factor(measured, NOW, "reasoning")
    assert before == after
    assert before < 0.5


def test_staleness_is_raised_by_age_past_one_half_life() -> None:
    result = evaluate_staleness(
        measured_at=NOW - timedelta(days=QUALITY_HALF_LIFE_DAYS + 1),
        now=NOW,
        capability_id="reasoning",
    )
    assert result.stale
    assert result.reason == "freshness"
    assert result.age_days == QUALITY_HALF_LIFE_DAYS + 1
    assert result.half_life_days == QUALITY_HALF_LIFE_DAYS


def test_fresh_evidence_is_not_stale_and_carries_no_reason() -> None:
    result = evaluate_staleness(
        measured_at=NOW - timedelta(days=3), now=NOW, capability_id="reasoning"
    )
    assert not result.stale
    assert result.reason is None
    assert result.age_days == 3


@pytest.mark.parametrize(
    ("drift_field", "superseded", "source_unreachable", "reason"),
    [
        (None, True, False, "superseded"),
        (None, False, True, "source_unreachable"),
        ("provider_version", False, False, "environment_drift:provider_version"),
    ],
)
def test_the_other_three_staleness_reasons_each_win_over_freshness(
    drift_field: str | None, superseded: bool, source_unreachable: bool, reason: str
) -> None:
    result = evaluate_staleness(
        measured_at=NOW - timedelta(days=1),
        now=NOW,
        capability_id="reasoning",
        drift_field=drift_field,
        superseded=superseded,
        source_unreachable=source_unreachable,
    )
    assert result.stale
    assert result.reason == reason


def test_superseded_outranks_every_other_reason() -> None:
    result = evaluate_staleness(
        measured_at=NOW - timedelta(days=400),
        now=NOW,
        capability_id="reasoning",
        drift_field="provider_version",
        superseded=True,
        source_unreachable=True,
    )
    assert result.reason == "superseded"


# --------------------------------------------------------------------------------------------
# Environment drift
# --------------------------------------------------------------------------------------------


def test_a_provider_version_change_is_drift_for_any_capability() -> None:
    measured = {"provider_kind": "ollama", "provider_version": "0.32.13"}
    current = {"provider_kind": "ollama", "provider_version": "0.33.0"}
    assert environment_drift(measured, current, capability_id="reasoning") == "provider_version"


def test_a_driver_change_is_drift_only_for_performance_capabilities() -> None:
    measured = {"provider_version": "0.32.13", "gpu_driver_version": "580.65.06"}
    current = {"provider_version": "0.32.13", "gpu_driver_version": "590.00.00"}
    assert environment_drift(measured, current, capability_id="reasoning") is None
    assert environment_drift(measured, current, capability_id="speed") == "gpu_driver_version"


def test_an_os_patch_level_is_never_drift() -> None:
    measured = {"provider_version": "0.32.13", "os_version": "Ubuntu 26.04 LTS"}
    current = {"provider_version": "0.32.13", "os_version": "Ubuntu 26.04.1 LTS"}
    assert environment_drift(measured, current, capability_id="speed") is None


def test_an_unreported_field_on_either_side_is_not_a_difference() -> None:
    assert (
        environment_drift(
            {"provider_version": None}, {"provider_version": "0.33.0"}, capability_id="reasoning"
        )
        is None
    )
    assert environment_drift(None, {"provider_version": "0.33.0"}, capability_id="speed") is None
    assert environment_drift({"provider_version": "0.32"}, None, capability_id="speed") is None


# --------------------------------------------------------------------------------------------
# Machine and profile separations
# --------------------------------------------------------------------------------------------


def test_quality_evidence_from_another_machine_is_admitted_and_performance_is_not() -> None:
    assert machine_admits("elsewhere", "here", "reasoning") is True
    assert machine_admits("elsewhere", "here", "speed") is False
    assert machine_admits("elsewhere", "here", "memory_efficiency") is False
    assert machine_admits("here", "here", "speed") is True


def test_an_unknown_local_fingerprint_admits_everything() -> None:
    """Not knowing which machine this is has not established that it is a different one."""
    assert machine_admits("elsewhere", None, "speed") is True


def test_profile_admits_is_equality_and_nothing_else() -> None:
    assert profile_admits("8f2c", "8f2c") is True
    assert profile_admits("8f2c", "4a91") is False


# --------------------------------------------------------------------------------------------
# Policy versions and selection — where "merging across benchmark versions" is prevented
# --------------------------------------------------------------------------------------------


def test_policy_versions_sort_numerically_where_they_can() -> None:
    assert policy_version_key("1.10.0") > policy_version_key("1.9.0")
    assert policy_version_key("2.0.0") > policy_version_key("1.99.99")
    assert policy_version_key("1.0.0") > policy_version_key("experimental")


def _candidate(**overrides: object) -> EvidenceCandidate:
    base: dict[str, object] = {
        "row_id": "R1",
        "capability_id": "reasoning",
        "runtime_profile_hash": "8f2c",
        "machine_fingerprint": "here",
        "policy_version": "1.0.0",
        "measured_at": NOW - timedelta(days=1),
        "score": 0.7,
        "confidence": 0.6,
        "sample_count": 40,
        "benchmark_versions": (("native.reasoning", "1.0.0"),),
    }
    base.update(overrides)
    return EvidenceCandidate(**base)  # type: ignore[arg-type]  # a homogeneous kwargs builder


def test_collapse_never_averages_two_records_it_selects_one() -> None:
    """The 'merging evidence across benchmark versions' failure mode, stated as a test.

    Two records for one subject differ in suite version, policy version and score. A merged
    result would be a number neither measurement produced.
    """
    old = _candidate(
        row_id="A",
        policy_version="1.0.0",
        score=0.2,
        benchmark_versions=(("native.reasoning", "1.0.0"),),
    )
    new = _candidate(
        row_id="B",
        policy_version="2.0.0",
        score=0.9,
        benchmark_versions=(("native.reasoning", "2.0.0"),),
    )
    selected = collapse_evidence([old, new], local_machine_fingerprint="here")
    assert len(selected) == 1
    assert selected[0].score in (0.2, 0.9)
    assert selected[0].score != pytest.approx(0.55)
    assert selected[0].policy_version == "2.0.0"
    assert selected[0].benchmark_versions == (("native.reasoning", "2.0.0"),)


def test_collapse_prefers_this_machine_over_a_higher_policy_version_elsewhere() -> None:
    local = _candidate(row_id="L", machine_fingerprint="here", policy_version="1.0.0")
    remote = _candidate(row_id="R", machine_fingerprint="elsewhere", policy_version="9.0.0")
    (selected,) = collapse_evidence([remote, local], local_machine_fingerprint="here")
    assert selected.row_id == "L"


def test_collapse_falls_back_to_measured_at_never_computed_at() -> None:
    older = _candidate(row_id="A", measured_at=NOW - timedelta(days=30))
    newer = _candidate(row_id="B", measured_at=NOW - timedelta(days=2))
    (selected,) = collapse_evidence([older, newer], local_machine_fingerprint="here")
    assert selected.row_id == "B"


def test_collapse_keeps_one_record_per_runtime_profile() -> None:
    here = _candidate(row_id="A", runtime_profile_hash="8f2c")
    elsewhere = _candidate(row_id="B", runtime_profile_hash="4a91")
    selected = collapse_evidence([here, elsewhere], local_machine_fingerprint="here")
    assert {row.runtime_profile_hash for row in selected} == {"8f2c", "4a91"}


def test_collapse_is_deterministic_under_input_order() -> None:
    rows = [
        _candidate(row_id="A", policy_version="1.0.0"),
        _candidate(row_id="B", policy_version="1.0.0", measured_at=NOW - timedelta(days=1)),
        _candidate(row_id="C", capability_id="coding"),
    ]
    forward = collapse_evidence(rows, local_machine_fingerprint="here")
    backward = collapse_evidence(list(reversed(rows)), local_machine_fingerprint="here")
    assert [row.row_id for row in forward] == [row.row_id for row in backward]


def test_collapse_of_nothing_is_nothing() -> None:
    assert collapse_evidence([], local_machine_fingerprint="here") == ()


# --------------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------------


def test_known_capability_defers_to_setspecs_vocabulary() -> None:
    assert known_capability("reasoning") is True
    assert known_capability("coding.python") is True
    assert known_capability("user.house_voice") is True
    assert known_capability("user") is False
    assert known_capability("not_a_capability") is False
