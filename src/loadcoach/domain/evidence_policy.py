"""loadcoach.domain.evidence_policy — the pure rules imported evidence obeys.

Every decision this module makes is a value function over values: what an evidence record binds
to, whether it has gone stale, whether it describes *this* machine, which of several records for
one subject is the one that scores, and whether a ``user.*`` capability is allowed to score at
all. No database, no HTTP, no clock — ``now`` is a parameter — so the binding table in
[ADR-0022 §4](../../adr/0022-capability-evidence-record-contract.md) is testable without an
importer, a FreeWeight or a schema.

Three rules are load-bearing enough to state here rather than only in a docstring below:

* **Freshness comes from ``measured_at``.** ADR-0022 §2 is explicit that ``computed_at`` never
  feeds it: a producer that re-aggregates four-month-old runs nightly must not thereby present
  them as new. :func:`freshness_factor` takes ``measured_at`` and has no parameter that would let
  a caller pass the other one by accident.
* **LoadCoach applies confidence; it never recomputes it** (ADR-0017). There is no confidence
  formula in this module and there must never be one. ``freshness_factor`` appears here only
  because ADR-0017's *staleness* surface is defined in terms of it ("stale when
  ``freshness_factor < 0.5``"), and a badge on a row is not a score in a decision.
* **Absence is not zero, and an exclusion is not an absence of information.** A record that
  cannot be used is named with a reason a person can act on — never dropped, never scored 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

import setspec

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

__all__ = [
    "EVIDENCE_FOREIGN_MACHINE",
    "EVIDENCE_PROFILE_MISMATCH",
    "EVIDENCE_UNBOUND",
    "MATCH_STATES",
    "PERFORMANCE_CAPABILITY_ROOTS",
    "PERFORMANCE_HALF_LIFE_DAYS",
    "QUALITY_HALF_LIFE_DAYS",
    "STALE_BELOW_FRESHNESS",
    "USER_CAPABILITY_ROOT",
    "Binding",
    "CalibrationFacts",
    "EvidenceCandidate",
    "EvidenceIdentity",
    "EvidenceOverview",
    "LocalModel",
    "MatchState",
    "Staleness",
    "bind_identity",
    "capability_half_life_days",
    "collapse_evidence",
    "environment_drift",
    "evaluate_staleness",
    "freshness_factor",
    "is_performance_capability",
    "is_user_capability",
    "known_capability",
    "machine_admits",
    "policy_version_key",
    "profile_admits",
    "user_capability_note",
    "user_goal_slug",
    "weights_admit",
]

MatchState = Literal["bound", "unmatched", "ambiguous_name_only"]
"""The three states ADR-0022 §4 defines. There is no fourth, and no ``None``."""

MATCH_STATES: Final[frozenset[str]] = frozenset({"bound", "unmatched", "ambiguous_name_only"})
"""The same three, as a set for a ``CHECK`` constraint and for validation."""

USER_CAPABILITY_ROOT: Final[str] = "user"
"""The reserved root ADR-0032 §1 gives user-authored goal evidence."""

QUALITY_HALF_LIFE_DAYS: Final[int] = 90
PERFORMANCE_HALF_LIFE_DAYS: Final[int] = 30
"""ADR-0017's two half-lives: quality is stable while the weights are, speed follows the
environment."""

FRESHNESS_FLOOR: Final[float] = 0.3
"""ADR-0017's floor. Old evidence decays toward a usable tiebreak rather than vanishing."""

STALE_BELOW_FRESHNESS: Final[float] = 0.5
"""ADR-0017's staleness surface: roughly one half-life."""

PERFORMANCE_CAPABILITY_ROOTS: Final[frozenset[str]] = frozenset(
    {"speed", "latency", "memory_efficiency", "energy_efficiency"}
)
"""The capability roots whose measurements are properties of *this machine* as much as of the
weights, and which ADR-0017 therefore hard-separates on ``machine_fingerprint``.

``token_efficiency`` is deliberately **not** here. It maps to FreeWeight's ``native.token_economy``
(benchmark catalog §6), which counts how many tokens a model spends to finish a task — a property
of the model's behaviour that does not change when the GPU does. Grouping it with the other three
``*_efficiency`` roots on the strength of its name would discard usable evidence for no
measurement reason, which is the failure ADR-0017's "discard aggressively" alternative was
rejected for.
"""

EVIDENCE_PROFILE_MISMATCH: Final[str] = "evidence_profile_mismatch"
EVIDENCE_FOREIGN_MACHINE: Final[str] = "evidence_foreign_machine"
EVIDENCE_UNBOUND: Final[str] = "evidence_unbound"
"""The three named exclusions. Each is an *absence with a reason*, counted toward ``low_evidence``
exactly as a capability nobody ever measured is (ADR-0023 §3, routing §5)."""

_DRIFT_FIELDS_ALL: Final[tuple[str, ...]] = ("provider_kind", "provider_version")
_DRIFT_FIELDS_PERFORMANCE: Final[tuple[str, ...]] = ("gpu_driver_version", "cuda_version")
"""Which environment fields count as drift, by capability kind.

``os_version`` is in neither: ADR-0017 scores an OS patch level at ``×1.0``, so a kernel update
is not a reason to badge every measurement on the machine as stale.
"""


@dataclass(frozen=True, slots=True)
class EvidenceIdentity:
    """The model identity an evidence record carries, denormalized (ADR-0022 §4).

    Attributes:
        provider_kind: e.g. ``"ollama"``.
        provider_model_name: The provider's own name for the weights.
        artifact_digest: ``sha256:…`` when the producer resolved one, else ``None``.
        canonical_id: The full ``provider/name@digest`` string (ADR-0008).
    """

    provider_kind: str
    provider_model_name: str
    artifact_digest: str | None
    canonical_id: str

    @property
    def is_name_only(self) -> bool:
        """Whether this identity names weights without proving which ones."""
        return self.artifact_digest is None


@dataclass(frozen=True, slots=True)
class LocalModel:
    """One row of the local registry, as much of it as binding needs.

    Named ``LocalModel`` rather than ``RegistryEntry`` because
    :class:`loadcoach.services.models.RegistryEntry` is a different thing — the presentation shape
    ``GET /models`` returns.

    Attributes:
        model_id: The registry row's ULID.
        provider_kind: e.g. ``"ollama"``.
        provider_model_name: The provider's own name.
        artifact_digest: The digest this row was last seen with, or ``None``.
        canonical_id: The row's canonical ID.
    """

    model_id: str
    provider_kind: str
    provider_model_name: str
    artifact_digest: str | None
    canonical_id: str


@dataclass(frozen=True, slots=True)
class Binding:
    """What one evidence identity resolves to against one registry.

    Attributes:
        match_state: ``"bound"``, ``"unmatched"`` or ``"ambiguous_name_only"``.
        model_id: The registry row bound to, or ``None`` when not bound.
        upgrade_model_id: A registry row that must be given ``upgrade_digest`` before the binding
            is true — ADR-0022 §4's second rule, the in-place identity upgrade. ``None`` when no
            upgrade is needed.
        upgrade_digest: The digest to write onto that row.
        note: Why, in one sentence, for the import report and the evidence page.
    """

    match_state: MatchState
    model_id: str | None
    upgrade_model_id: str | None
    upgrade_digest: str | None
    note: str

    @property
    def is_bound(self) -> bool:
        """Whether this evidence contributes to routing at all (ADR-0022 §4)."""
        return self.match_state == "bound"


def bind_identity(identity: EvidenceIdentity, registry: Sequence[LocalModel]) -> Binding:
    """Resolve one evidence identity against the local registry (ADR-0022 §4).

    The four rules, in the order the ADR's table gives them:

    1. **Exact triple match** — same ``(provider_kind, provider_model_name, artifact_digest)``,
       where two ``None`` digests match each other. Bound.
    2. **A digest in the bundle against a local ``name_only`` row** for the same
       ``(kind, name)``. The registry row is upgraded with the digest and then bound; the caller
       performs the upgrade, this function only says which row and which digest.
    3. **``name_only`` evidence against a locally-digested row.** *Not* bound:
       ``ambiguous_name_only``. The weights cannot be proven to be the ones installed, and a
       plausible score attached to the wrong weights is worse than no score.
    4. **No candidate at all.** ``unmatched``, retained, bound automatically the next time
       discovery produces a match — with no re-import.

    Args:
        identity: The identity the evidence record carries.
        registry: Every local model row. Order does not affect the result.

    Returns:
        The :class:`Binding`. Never raises and never reports failure: an identity that matches
        nothing is a legitimate, retained state, not an error (ADR-0022, rejected alternatives).
    """
    same_name = [
        row
        for row in registry
        if row.provider_kind == identity.provider_kind
        and row.provider_model_name == identity.provider_model_name
    ]

    for row in same_name:
        if row.artifact_digest == identity.artifact_digest:
            return Binding(
                match_state="bound",
                model_id=row.model_id,
                upgrade_model_id=None,
                upgrade_digest=None,
                note="identity triple matches a discovered model exactly",
            )

    if identity.artifact_digest is not None:
        for row in same_name:
            if row.artifact_digest is None:
                return Binding(
                    match_state="bound",
                    model_id=row.model_id,
                    upgrade_model_id=row.model_id,
                    upgrade_digest=identity.artifact_digest,
                    note=(
                        "the bundle carried a digest for a model this registry knew by name "
                        f"only; the registry row was upgraded to {identity.artifact_digest}"
                    ),
                )
        return Binding(
            match_state="unmatched",
            model_id=None,
            upgrade_model_id=None,
            upgrade_digest=None,
            note=(
                "no discovered model carries this identity; retained and bound automatically "
                "when discovery next produces a match"
            ),
        )

    if same_name:
        return Binding(
            match_state="ambiguous_name_only",
            model_id=None,
            upgrade_model_id=None,
            upgrade_digest=None,
            note=(
                "the bundle identified the model by name only, and this registry holds a digest "
                "for that name; the measured weights cannot be proven to be the installed ones"
            ),
        )

    return Binding(
        match_state="unmatched",
        model_id=None,
        upgrade_model_id=None,
        upgrade_digest=None,
        note=(
            "no discovered model carries this identity; retained and bound automatically when "
            "discovery next produces a match"
        ),
    )


def is_user_capability(capability_id: str) -> bool:
    """Report whether ``capability_id`` lives under the reserved ``user`` root (ADR-0032 §1).

    Args:
        capability_id: The capability term.

    Returns:
        ``True`` for ``user.<slug>``; ``False`` for the bare root ``user``, which SetSpec's
        vocabulary refuses as a capability in its own right, and for everything else.
    """
    root, separator, slug = capability_id.partition(".")
    return root == USER_CAPABILITY_ROOT and bool(separator) and bool(slug)


def user_goal_slug(capability_id: str) -> str | None:
    """Return the goal slug behind a ``user.*`` capability, or ``None``.

    Args:
        capability_id: The capability term.

    Returns:
        Everything after the first dot for a ``user.*`` term; ``None`` otherwise.
    """
    if not is_user_capability(capability_id):
        return None
    return capability_id.partition(".")[2]


def weights_admit(capability_id: str, weights: Mapping[str, float]) -> bool:
    """Report whether a task profile lets this capability influence routing at all.

    The ``user.*`` opt-in, stated once (ADR-0032 §6, spec §11.3a): *LoadCoach does not weight a
    ``user.*`` capability unless a task profile names it explicitly.* A capability that one
    person's taste defines must not acquire routing influence by existing.

    Every other capability is admitted here and then weighted — or not — by whether the profile
    gives it a weight, exactly as before. This function is the gate, not the weighting.

    Args:
        capability_id: The capability term.
        weights: The active task profile's capability weights.

    Returns:
        ``True`` unless ``capability_id`` is a ``user.*`` term the profile does not name.
    """
    if not is_user_capability(capability_id):
        return True
    return capability_id in weights


def is_performance_capability(capability_id: str) -> bool:
    """Report whether this capability's measurements describe the machine as well as the model.

    Args:
        capability_id: The capability term, possibly a specialization such as ``speed.decode``.

    Returns:
        ``True`` when the term's **root** is in :data:`PERFORMANCE_CAPABILITY_ROOTS`.
    """
    return capability_id.partition(".")[0] in PERFORMANCE_CAPABILITY_ROOTS


def capability_half_life_days(capability_id: str) -> int:
    """Return ADR-0017's half-life for this capability's kind.

    Args:
        capability_id: The capability term.

    Returns:
        :data:`PERFORMANCE_HALF_LIFE_DAYS` for a performance/memory/energy capability,
        :data:`QUALITY_HALF_LIFE_DAYS` otherwise.
    """
    return (
        PERFORMANCE_HALF_LIFE_DAYS
        if is_performance_capability(capability_id)
        else QUALITY_HALF_LIFE_DAYS
    )


def freshness_factor(measured_at: datetime, now: datetime, capability_id: str) -> float:
    """Return ``0.5 ** (age_days / half_life_days)``, floored, from **``measured_at``**.

    This is not a confidence computation and must never become one: FreeWeight computed the
    confidence on the record and LoadCoach applies that number unchanged (ADR-0017). What this
    supports is ADR-0017's *staleness surface*, which is defined in terms of the same decay —
    a badge on a row, never a multiplier in a decision.

    Args:
        measured_at: The latest ``completed_at`` among the contributing runs. There is
            deliberately no parameter for ``computed_at``: recomputation must not look like
            re-measurement (ADR-0022 §2).
        now: The current instant.
        capability_id: Selects the half-life.

    Returns:
        The factor, in ``[FRESHNESS_FLOOR, 1.0]``. Evidence dated in the future — a producer whose
        clock runs ahead — is treated as brand new (``1.0``) rather than as more-than-fresh.
    """
    age_days = max(0.0, (now - measured_at).total_seconds() / 86400.0)
    decayed: float = 0.5 ** (age_days / capability_half_life_days(capability_id))
    return max(FRESHNESS_FLOOR, min(1.0, decayed))


def environment_drift(
    measured: Mapping[str, object] | None,
    current: Mapping[str, object] | None,
    *,
    capability_id: str,
) -> str | None:
    """Name the environment field that has changed since a measurement, if any.

    ADR-0017's ``environment_factor`` reduces *confidence* for drift, and confidence is
    FreeWeight's to compute. What LoadCoach owns is the staleness badge that the same drift
    raises, so this function answers "has anything changed?" and never "by how much?".

    Args:
        measured: The environment recorded on the evidence record.
        current: This machine's environment now.
        capability_id: Decides whether driver/CUDA changes count — they describe how fast the
            hardware runs, not how well the weights reason.

    Returns:
        The first drifting field name, checked in a fixed order, or ``None``. A field absent or
        ``None`` on **either** side is never a drift: an unreported value is not a different one
        (ADR-0016).
    """
    if not measured or not current:
        return None
    fields = _DRIFT_FIELDS_ALL
    if is_performance_capability(capability_id):
        fields = fields + _DRIFT_FIELDS_PERFORMANCE
    for field in fields:
        before = measured.get(field)
        after = current.get(field)
        if before is None or after is None:
            continue
        if before != after:
            return field
    return None


@dataclass(frozen=True, slots=True)
class Staleness:
    """Whether one evidence record should be shown, and used, with a staleness badge.

    Attributes:
        stale: The badge itself.
        reason: A short code — ``"superseded"``, ``"source_unreachable"``,
            ``"environment_drift:<field>"`` or ``"freshness"`` — or ``None`` when fresh. Stored in
            ``capability_evidence.stale_reason`` verbatim.
        age_days: Whole days between ``measured_at`` and ``now``, never negative.
        freshness_factor: ADR-0017's decay at that age, for the UI's "why".
        half_life_days: Which half-life produced it.
    """

    stale: bool
    reason: str | None
    age_days: int
    freshness_factor: float
    half_life_days: int


def evaluate_staleness(
    *,
    measured_at: datetime,
    now: datetime,
    capability_id: str,
    drift_field: str | None = None,
    superseded: bool = False,
    source_unreachable: bool = False,
) -> Staleness:
    """Decide whether one record is stale, and say which of the four reasons applies.

    Reasons are checked most-important first, because a row has one badge and the most actionable
    reason is the one worth showing: a superseded row will not come back on its own, an
    unreachable source is an operational fault, drift is a re-run, and age is only a decay.

    Args:
        measured_at: When the contributing runs finished — never ``computed_at``.
        now: The current instant.
        capability_id: Selects the half-life.
        drift_field: The output of :func:`environment_drift`, or ``None``.
        superseded: Whether a complete bundle from this source omitted the record
            (ADR-0022 §5). Superseded rows are marked, never deleted.
        source_unreachable: Whether the source could not be reached on the last refresh, which is
            the state P6's degradation contract requires the UI and ``/health`` to show.

    Returns:
        The :class:`Staleness`.
    """
    age_days = max(0, int((now - measured_at).total_seconds() // 86400))
    factor = freshness_factor(measured_at, now, capability_id)
    half_life = capability_half_life_days(capability_id)
    if superseded:
        reason = "superseded"
    elif source_unreachable:
        reason = "source_unreachable"
    elif drift_field is not None:
        reason = f"environment_drift:{drift_field}"
    elif factor < STALE_BELOW_FRESHNESS:
        reason = "freshness"
    else:
        return Staleness(
            stale=False,
            reason=None,
            age_days=age_days,
            freshness_factor=factor,
            half_life_days=half_life,
        )
    return Staleness(
        stale=True,
        reason=reason,
        age_days=age_days,
        freshness_factor=factor,
        half_life_days=half_life,
    )


def machine_admits(
    evidence_fingerprint: str, local_fingerprint: str | None, capability_id: str
) -> bool:
    """Report whether evidence measured on ``evidence_fingerprint`` may score here.

    ADR-0017's last hard separation: *the machine fingerprint differs **and** the metric is
    performance/memory/energy*. Quality measured elsewhere is retained and used with a machine
    badge — the weights reason the same on any card. Throughput measured elsewhere describes that
    card, and using it here would be a fabricated measurement of this one.

    Args:
        evidence_fingerprint: Where the record was measured.
        local_fingerprint: This machine's fingerprint, or ``None`` when SweatMeter could not
            produce one.
        capability_id: The capability being scored.

    Returns:
        ``True`` when the record may score. An unknown local fingerprint admits everything: not
        knowing which machine this is has not established that it is a different one (ADR-0016).
    """
    if local_fingerprint is None or evidence_fingerprint == local_fingerprint:
        return True
    return not is_performance_capability(capability_id)


def profile_admits(evidence_profile_hash: str, candidate_profile_hash: str) -> bool:
    """Report whether a measurement describes the profile a candidate will run under.

    ADR-0023 §3's hard separation, as one named predicate so that "the hashes are equal" is
    written once and read everywhere.

    Args:
        evidence_profile_hash: The profile the measurement was taken under.
        candidate_profile_hash: The profile routing resolved for this execution.

    Returns:
        ``True`` iff they are the same profile.
    """
    return evidence_profile_hash == candidate_profile_hash


def policy_version_key(policy_version: str) -> tuple[tuple[int, ...], str]:
    """Return a sort key that orders confidence-policy versions numerically where it can.

    ADR-0022 §3: *routing uses the highest ``policy_version`` present for a subject*. Versions are
    free-form strings on the wire, so ``"1.10.0"`` must sort above ``"1.9.0"`` when both are
    dotted integers, and anything else falls back to a stable lexicographic order rather than
    raising on a producer's naming choice.

    Args:
        policy_version: The version string.

    Returns:
        ``(numeric parts, original string)``. A non-numeric version sorts below every numeric one,
        which is the conservative direction: an unrecognized policy does not displace a known one.
    """
    parts = policy_version.split(".")
    if parts and all(part.isdigit() for part in parts):
        return (tuple(int(part) for part in parts), policy_version)
    return ((), policy_version)


@dataclass(frozen=True, slots=True)
class CalibrationFacts:
    """What ADR-0032 §6 requires a ``user.*`` explanation to say, in the words it says them in.

    Attributes:
        kappa_w: Quadratic-weighted agreement between the judge and the person whose goal it is.
        n_holdout: How many graded samples the judge was never shown. Travels with ``kappa_w``
            everywhere, because an agreement coefficient without its holdout is not a measurement.
        graded_by: Free text the user supplied naming the grader.
        measured_at: When the calibration was measured.
    """

    kappa_w: float
    n_holdout: int
    graded_by: str
    measured_at: datetime


def user_capability_note(capability_id: str, calibration: CalibrationFacts | None) -> str:
    """Render the sentence a decision that used a ``user.*`` capability must carry.

    ADR-0032 §6 asks for the goal, its ``kappa_w`` and its ``n_holdout`` **in words** — not only
    as numbers in the machine-readable breakdown — because "confidence 0.31" is not something a
    person can audit and "judge agreement kappa_w 0.74 over 18 held-out samples you graded" is.

    Args:
        capability_id: The ``user.<slug>`` term.
        calibration: The judge's measured agreement, or ``None`` for a goal scored entirely by
            rules, where no judge was involved and there is nothing to agree about.

    Returns:
        A single sentence naming the goal slug, and — when there was a judge — ``kappa_w``, the
        holdout size, who graded it and when.
    """
    slug = user_goal_slug(capability_id) or capability_id
    opening = (
        f"user-defined goal {slug!r}, weighted because this task profile names it explicitly "
        "(ADR-0032 §6)"
    )
    if calibration is None:
        return f"{opening}; scored entirely by rules, so no judge agreement was measured"
    return (
        f"{opening}; judge agreement kappa_w {calibration.kappa_w:.2f} over "
        f"{calibration.n_holdout} held-out samples graded by {calibration.graded_by} on "
        f"{calibration.measured_at.date().isoformat()}"
    )


@dataclass(frozen=True, slots=True)
class EvidenceOverview:
    """What routing §8's ``evidence_summary`` needs from the store rather than from a candidate.

    Attributes:
        configured: Whether ``[evidence] freeweight_url`` names a source at all. ``False`` is
            **not configured**, which is a different state from unavailable and reads differently
            everywhere.
        source_status: The configured source's last outcome — ``ok``, ``unreachable``,
            ``refused``, ``failed`` — or ``None`` when nothing has been attempted.
        rows: How many evidence rows exist, in any ``match_state``.
        bound: How many contribute to routing.
        unmatched: Retained for a model discovery has not seen.
        ambiguous: Retained ``ambiguous_name_only``; never scores.
        stale: How many carry a staleness badge.
        imported_at: The most recent import.
        generated_at: The producer's own timestamp for that bundle.
        oldest_measured_at: The oldest measurement in the store.
        newest_measured_at: The newest.
        bundle_schema_version: The bundle version last imported.
        policy_version: The highest confidence-policy version present (ADR-0022 §3).
        vocabulary_version: The highest capability-vocabulary version present.
        error_text: The last failure's message, when there is one.
    """

    configured: bool = False
    source_status: str | None = None
    rows: int = 0
    bound: int = 0
    unmatched: int = 0
    ambiguous: int = 0
    stale: int = 0
    imported_at: datetime | None = None
    generated_at: datetime | None = None
    oldest_measured_at: datetime | None = None
    newest_measured_at: datetime | None = None
    bundle_schema_version: str | None = None
    policy_version: str | None = None
    vocabulary_version: str | None = None
    error_text: str | None = None

    @property
    def status(self) -> str:
        """The one word the UI, ``/health`` and the explanation all use for this state."""
        if not self.configured and self.rows == 0:
            return "not_configured"
        if self.source_status in ("unreachable", "refused", "failed"):
            return self.source_status
        if self.rows == 0:
            return "none"
        return "ok"

    @property
    def note(self) -> str:
        """One sentence a person can read, for the explanation and the evidence page."""
        if self.status == "not_configured":
            return (
                "No evidence source is configured ([evidence] freeweight_url is empty), so "
                "routing ranks on declared capabilities and priors and says so."
            )
        if self.status == "none":
            return (
                "An evidence source is configured but nothing has been imported from it yet; "
                "routing ranks on declared capabilities and priors."
            )
        if self.status == "unreachable":
            return (
                f"FreeWeight could not be reached, so the last import is retained and marked "
                f"stale ({self.stale} of {self.rows} records); routing continues on it and on "
                "its priors."
            )
        if self.status in ("refused", "failed"):
            return (
                f"The last refresh was {self.status}: {self.error_text or 'no detail recorded'}. "
                f"The previous import's {self.rows} records are retained and still in use."
            )
        return (
            f"{self.bound} of {self.rows} imported records are bound to a discovered model "
            f"({self.stale} stale, {self.unmatched} unmatched, {self.ambiguous} ambiguous)."
        )


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    """One stored evidence row, reduced to what selection and scoring need.

    Attributes:
        row_id: The ``capability_evidence`` ULID — the deterministic last tiebreak.
        capability_id: The capability measured.
        runtime_profile_hash: The profile it was measured under.
        machine_fingerprint: The machine it was measured on.
        policy_version: The confidence policy it was computed under.
        measured_at: The latest ``completed_at`` among the contributing runs.
        score: The measured ability.
        confidence: FreeWeight's number, applied unchanged.
        sample_count: Supported samples behind ``score``.
        benchmark_versions: ``(suite key, version)`` pairs, sorted. Carried so that a decision can
            name the measurement it used, and so that two records from different suite versions
            are visibly two measurements rather than one blended number.
        calibration: Present only for a judged ``user.*`` record.
    """

    row_id: str
    capability_id: str
    runtime_profile_hash: str
    machine_fingerprint: str
    policy_version: str
    measured_at: datetime
    score: float
    confidence: float
    sample_count: int
    benchmark_versions: tuple[tuple[str, str], ...] = ()
    calibration: CalibrationFacts | None = None


def collapse_evidence(
    candidates: Iterable[EvidenceCandidate], *, local_machine_fingerprint: str | None
) -> tuple[EvidenceCandidate, ...]:
    """Reduce many rows for one model to **at most one per (capability, runtime profile)**.

    This is where "merging evidence across benchmark versions" is prevented, and it is prevented
    by never merging anything: the function *selects*, and every returned number is a number some
    single record carried. Averaging two records taken under different suite versions, different
    policies or on different machines would produce a figure no measurement ever produced —
    exactly the fabricated value the suite refuses everywhere else.

    Within a ``(capability_id, runtime_profile_hash)`` group the order is:

    1. this machine before any other (ADR-0017's machine separation, applied as a preference here
       and as a refusal in :func:`machine_admits`);
    2. the highest ``policy_version`` (ADR-0022 §3);
    3. the latest ``measured_at`` — never ``computed_at``;
    4. the row ULID, so the result is deterministic when every earlier key ties.

    Rows for a *different* runtime profile are kept, one per profile, so that scoring can report
    ``evidence_profile_mismatch`` with the hash a measurement actually exists for rather than
    silently reporting no evidence at all.

    Args:
        candidates: Every stored row for one model.
        local_machine_fingerprint: This machine's fingerprint, or ``None`` if unknown.

    Returns:
        The selected rows, ordered by capability then profile hash.
    """
    grouped: dict[tuple[str, str], list[EvidenceCandidate]] = {}
    for candidate in candidates:
        key = (candidate.capability_id, candidate.runtime_profile_hash)
        grouped.setdefault(key, []).append(candidate)

    def best_of(group: list[EvidenceCandidate]) -> EvidenceCandidate:
        return max(group, key=lambda row: _selection_key(row, local_machine_fingerprint))

    return tuple(best_of(group) for _key, group in sorted(grouped.items()))


def _selection_key(
    candidate: EvidenceCandidate, local_machine_fingerprint: str | None
) -> tuple[int, tuple[tuple[int, ...], str], datetime, str]:
    """Return the key :func:`collapse_evidence` maximizes; larger is preferred throughout."""
    is_local = (
        local_machine_fingerprint is None
        or candidate.machine_fingerprint == local_machine_fingerprint
    )
    return (
        1 if is_local else 0,
        policy_version_key(candidate.policy_version),
        candidate.measured_at,
        candidate.row_id,
    )


def known_capability(capability_id: str) -> bool:
    """Report whether SetSpec's vocabulary recognizes this capability's root.

    A thin, named wrapper so that "is this term real?" is answered by the package that owns the
    vocabulary rather than by a set literal that would drift from it (SetSpec spec §4).

    Args:
        capability_id: The capability term.

    Returns:
        ``True`` when the root is known and the term is syntactically valid.
    """
    return setspec.is_known_capability(capability_id)
