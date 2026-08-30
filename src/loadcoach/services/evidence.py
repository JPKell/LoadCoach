"""loadcoach.services.evidence — importing a FreeWeight bundle, and keeping it bound.

Everything here treats the bundle as **untrusted input** (spec §14): it is size-limited before it
is parsed, version-negotiated before anything is written, validated record by record against
SetSpec's own models, and stored without ever being executed or allowed to touch a task profile.

Three properties are the ones worth stating up front, because each is a way a plausible importer
gets it wrong:

* **Version rejection does not partially parse.** The schema major is decided *before* the
  transaction opens, so an unsupported bundle leaves every existing row byte-identical. There is
  no half-imported state to clean up, because none is ever entered.
* **Nothing is merged.** Two records that land on the same uniqueness key inside one bundle are a
  producer bug, not an invitation to average: the second is *rejected by name*, so
  "merging evidence across benchmark versions" cannot happen quietly.
* **An unknown model is not a failure.** Import never fails because discovery has not seen a
  model; the row is retained with its ``match_state`` and bound the next time discovery produces
  a match, with no re-import (ADR-0022 §4).

The field-level provenance rules — that ``canonical_id`` is a pure function of the identity
triple, that ``measured_at`` cannot follow ``computed_at``, that ``capability_id`` is a term in the
vocabulary at its declared version — belong to SetSpec's own models and are **not** re-implemented
here. Re-checking them locally would create a second, drifting definition of the contract, which
is the failure ADR-0022 was written after finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Final

import setspec
from baseaicore import SuiteError, is_supported
from pydantic import ValidationError as PydanticValidationError
from setspec import SchemaVersion, load_envelope
from setspec.capability.v1 import CapabilityEvidenceFields, CapabilityEvidenceIn
from weightsdb import upsert

from loadcoach.domain.evidence_policy import (
    EvidenceIdentity,
    LocalModel,
    bind_identity,
    environment_drift,
    evaluate_staleness,
)
from loadcoach.infrastructure.db.models import CapabilityEvidence, EvidenceSource, Model

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from sqlalchemy.orm import Session

    from loadcoach.services.database import Database

__all__ = [
    "BUNDLE_SCHEMA",
    "MAX_IMPORT_BYTES",
    "MAX_PARSE_BYTES",
    "EvidenceImportFailed",
    "EvidenceSchemaVersionUnsupported",
    "ImportOutcome",
    "RejectedRecord",
    "RebindOutcome",
    "import_bundle",
    "rebind_evidence",
    "rebind_evidence_in",
]

BUNDLE_SCHEMA: Final[str] = "benchmark.evidence_bundle"
"""The one payload type ``POST /evidence/import`` accepts."""

MAX_IMPORT_BYTES: Final[int] = 128 * 1024 * 1024
"""ADR-0026 §3's import limit, enforced **during streaming** by the fetch client.

This is a transfer cap: it bounds what LoadCoach will pull over the network before it has any
idea what the body is. It is deliberately larger than :data:`MAX_PARSE_BYTES`, and the two mean
different things — see that constant.
"""

MAX_PARSE_BYTES: Final[int] = setspec.MAX_PAYLOAD_BYTES
"""What may actually be parsed: SetSpec's own envelope guard, not raised.

ADR-0026 §3 names 128 MiB as the import limit and SetSpec caps an envelope at 16 MiB. Both are
kept, because they answer different questions — "how much will we read from a stranger?" and "how
much JSON will we build objects from?" — and raising a shared package's own guard eightfold from
a consumer is precisely the local weakening the suite refuses everywhere else. At roughly two
kilobytes a record, 16 MiB is some eight thousand
``(model, profile, machine, capability)`` combinations, which is far past what any one machine
produces; if a real bundle ever exceeds it, the fix is a SetSpec change, not an override here.
"""


class EvidenceImportFailed(SuiteError):
    """The bundle itself was unusable (spec §13).

    Distinct from :class:`~loadcoach.infrastructure.freeweight_client.EvidenceSourceRefused`,
    which means LoadCoach declined to fetch the URL at all.
    """

    code: ClassVar[str] = "EVIDENCE_IMPORT_FAILED"


class EvidenceSchemaVersionUnsupported(SuiteError):
    """The bundle declares a schema major this build cannot read (api.md §7, spec §13).

    ``details`` names **both** versions — what arrived and what is accepted — because a consumer
    that only learns "unsupported" cannot tell whether to upgrade itself or its producer. Raised
    before any database work begins, so existing evidence is untouched by construction.
    """

    code: ClassVar[str] = "SCHEMA_VERSION_UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class RejectedRecord:
    """One record that could not be imported, and why.

    Attributes:
        index: Its position in the bundle's ``evidence`` list, so a producer can find it.
        canonical_id: The identity it claimed, when it claimed a readable one.
        capability_id: The capability it claimed, when it claimed one.
        reason: A short code — ``"INVALID_RECORD"`` or ``"DUPLICATE_RECORD"``.
        detail: One human-readable sentence.
    """

    index: int
    canonical_id: str | None
    capability_id: str | None
    reason: str
    detail: str

    def as_json(self) -> dict[str, Any]:
        """Return this rejection as the API reports it."""
        return {
            "index": self.index,
            "canonical_id": self.canonical_id,
            "capability_id": self.capability_id,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """What one import did, counted the way api.md §7 reports it.

    Attributes:
        source_row_id: The ``evidence_sources`` ULID this bundle was filed under.
        source_key: The producer's own ``source_id``.
        schema_version: The bundle's declared ``MAJOR.MINOR``.
        generated_at: The envelope's timestamp — what a client stores and sends back as its next
            ``?since=`` (ADR-0022 §5). Never LoadCoach's own clock.
        complete: Whether the producer declared a full export.
        total: How many records the bundle carried.
        imported: New rows.
        updated: Rows that already existed under the same uniqueness key.
        unmatched: Rows retained with ``match_state="unmatched"``.
        ambiguous: Rows retained with ``match_state="ambiguous_name_only"``.
        bound: Rows that scored — ``match_state="bound"``.
        upgraded_models: Registry rows given a digest by this bundle (ADR-0022 §4, rule 2).
        superseded: Rows this source had that a **complete** bundle omitted; marked, never
            deleted.
        rejected: Every record that could not be imported, with its reason.
    """

    source_row_id: str
    source_key: str
    schema_version: str
    generated_at: datetime | None
    complete: bool
    total: int
    imported: int = 0
    updated: int = 0
    unmatched: int = 0
    ambiguous: int = 0
    bound: int = 0
    upgraded_models: int = 0
    superseded: int = 0
    rejected: tuple[RejectedRecord, ...] = field(default_factory=tuple)

    def as_json(self) -> dict[str, Any]:
        """Return the ``POST /evidence/import`` response body."""
        from baseaicore.timeutil import to_rfc3339

        return {
            "source_id": self.source_key,
            "source_row_id": self.source_row_id,
            "schema_version": self.schema_version,
            "generated_at": None if self.generated_at is None else to_rfc3339(self.generated_at),
            "complete": self.complete,
            "total": self.total,
            "imported": self.imported,
            "updated": self.updated,
            "unmatched": self.unmatched,
            "ambiguous_name_only": self.ambiguous,
            "bound": self.bound,
            "upgraded_models": self.upgraded_models,
            "superseded": self.superseded,
            "rejected": [item.as_json() for item in self.rejected],
        }


@dataclass(frozen=True, slots=True)
class RebindOutcome:
    """What one re-binding pass changed (ADR-0022 §4: re-evaluated on every discovery pass)."""

    bound: int = 0
    unbound: int = 0
    upgraded_models: int = 0
    examined: int = 0


def _accepted_versions(accept_schema_majors: Sequence[int]) -> list[SchemaVersion]:
    """Turn the configured majors into the version list ``load_envelope`` negotiates against.

    Acceptance is by **major** (ADR-0009 rule 9), so listing ``1.0`` accepts ``1.7`` too; the
    minor here is only a placeholder the reader policy never compares.
    """
    return [SchemaVersion(major, 0) for major in accept_schema_majors]


def _identity_of(record: CapabilityEvidenceFields) -> EvidenceIdentity:
    """Lift one validated record's model identity into the domain's value object."""
    identity = record.model
    return EvidenceIdentity(
        provider_kind=identity.provider_kind,
        provider_model_name=identity.provider_model_name,
        artifact_digest=identity.artifact_digest,
        canonical_id=identity.canonical_id,
    )


def _registry_of(session: Session) -> list[LocalModel]:
    """Read every registry row binding needs, once per transaction."""
    return [
        LocalModel(
            model_id=row.id,
            provider_kind=row.provider_kind,
            provider_model_name=row.provider_model_name,
            artifact_digest=row.artifact_digest,
            canonical_id=row.canonical_id,
        )
        for row in session.query(Model).all()
    ]


def _uniqueness_key(record: CapabilityEvidenceFields, source_row_id: str) -> tuple[str, ...]:
    """The consumer-side uniqueness key from ADR-0022 §3, as a tuple."""
    return (
        source_row_id,
        record.model.canonical_id,
        record.runtime_profile_hash,
        record.machine_fingerprint,
        record.capability_id,
        record.policy_version,
    )


def _mapping_or_none(value: object) -> dict[str, Any] | None:
    """Return a plain JSON-able mapping, or ``None`` for an empty one."""
    if not value:
        return None
    if isinstance(value, dict):
        return dict(value)
    return None


def _goal_json(record: CapabilityEvidenceFields) -> dict[str, Any] | None:
    """Collect ADR-0032 §5's goal-sourced group, or ``None`` on a non-goal record.

    Stored as one JSON column rather than seven, because every field arrives or none does and a
    consumer that splits them has to reason about six impossible combinations.
    """
    if record.goal_hash is None and record.calibration is None and record.judge_set is None:
        return None
    dumped = record.model_dump(mode="json")
    goal: dict[str, Any] = {"judge_validity_factor": record.judge_validity_factor}
    for key in (
        "goal_hash",
        "goal_pack_version",
        "score_method_mix",
        "judge_set",
        "calibration",
        "uncalibrated",
    ):
        if dumped.get(key) is not None:
            goal[key] = dumped[key]
    return goal


def _upsert_source(
    session: Session,
    *,
    source_key: str,
    kind: str,
    url: str | None,
    schema_version: str,
    generated_at: datetime | None,
    now: datetime,
) -> EvidenceSource:
    """Insert or refresh the ``evidence_sources`` row this bundle belongs to."""
    existing = session.query(EvidenceSource).filter_by(source_key=source_key).one_or_none()
    if existing is None:
        existing = EvidenceSource(
            source_key=source_key,
            kind=kind,
            url=url,
            record_count=0,
            created_at=now,
        )
        session.add(existing)
    existing.kind = kind
    if url is not None:
        existing.url = url
    existing.last_import_at = now
    existing.last_status = "ok"
    existing.schema_version = schema_version
    existing.error_text = None
    existing.generated_at = generated_at
    session.flush()
    return existing


def import_bundle(  # noqa: PLR0913 — every argument is a documented import input
    database: Database,
    document: bytes | str | Mapping[str, Any],
    *,
    now: datetime,
    accept_schema_majors: Sequence[int] = (1,),
    source_kind: str = "file",
    url: str | None = None,
    current_environment: Mapping[str, Any] | None = None,
) -> ImportOutcome:
    """Import one ``benchmark.evidence_bundle`` in a single transaction.

    The order of the first three steps is the atomicity claim, and it is deliberate: size,
    then schema version, then per-record validation — **all before ``database.write()`` is
    entered**. A bundle rejected for its version therefore cannot have written anything, and the
    rows an earlier import left are byte-identical afterwards.

    Args:
        database: The application's database handle.
        document: The bundle, as bytes, text or an already-parsed mapping.
        now: The import instant. Injected, so a test can age evidence deterministically.
        accept_schema_majors: Which schema majors this installation reads
            (``[evidence] accept_schema_majors``).
        source_kind: ``"freeweight_api"``, ``"file"`` or ``"manual"``.
        url: The URL the bundle was pulled from, when it was pulled.
        current_environment: This machine's provider/driver facts, for the drift half of
            staleness. ``None`` disables drift detection rather than assuming no drift.

    Returns:
        The :class:`ImportOutcome`, with per-record counts and every rejection named.

    Raises:
        EvidenceSchemaVersionUnsupported: The bundle's major is not in ``accept_schema_majors``.
            ``details`` names both versions and nothing has been written.
        EvidenceImportFailed: The document is oversized, unparsable, not an evidence bundle, or
            its envelope is malformed. Nothing has been written.
    """
    envelope = _negotiate(document, accept_schema_majors=accept_schema_majors)
    payload = envelope.payload
    if not isinstance(payload, dict):
        raise EvidenceImportFailed(
            "An evidence bundle's payload must be an object.",
            details={"received_type": type(payload).__name__},
        )
    source_key = payload.get("source_id")
    complete = payload.get("complete")
    if not isinstance(source_key, str) or not source_key:
        raise EvidenceImportFailed(
            "An evidence bundle must name the source that produced it (ADR-0022 §5).",
            details={"field": "source_id"},
        )
    if not isinstance(complete, bool):
        raise EvidenceImportFailed(
            "An evidence bundle must declare whether it is complete: only a complete bundle "
            "lets a consumer infer removals (ADR-0022 §5).",
            details={"field": "complete"},
        )
    raw_records = payload.get("evidence", [])
    if not isinstance(raw_records, list):
        raise EvidenceImportFailed(
            "An evidence bundle's `evidence` must be a list.",
            details={"field": "evidence", "received_type": type(raw_records).__name__},
        )

    validated: list[tuple[int, CapabilityEvidenceFields]] = []
    rejected: list[RejectedRecord] = []
    for index, raw in enumerate(raw_records):
        try:
            validated.append((index, CapabilityEvidenceIn.model_validate(raw)))
        except PydanticValidationError as exc:
            rejected.append(
                RejectedRecord(
                    index=index,
                    canonical_id=_claimed(raw, "model", "canonical_id"),
                    capability_id=_claimed(raw, "capability_id"),
                    reason="INVALID_RECORD",
                    detail=_first_error(exc),
                )
            )

    return _write(
        database,
        validated=validated,
        rejected=rejected,
        source_key=source_key,
        source_kind=source_kind,
        url=url,
        complete=complete,
        total=len(raw_records),
        schema_version=envelope.schema_version,
        generated_at=envelope.generated_at,
        now=now,
        current_environment=current_environment,
    )


def _negotiate(
    document: bytes | str | Mapping[str, Any], *, accept_schema_majors: Sequence[int]
) -> Any:  # noqa: ANN401 — setspec.SchemaEnvelope is generic in its payload
    """Decide the schema version, before anything can be written.

    Raises:
        EvidenceSchemaVersionUnsupported: The major is not accepted; both versions are named.
        EvidenceImportFailed: The document is oversized, unparsable or not a bundle.
    """
    if isinstance(document, (bytes, str)) and len(document) > MAX_PARSE_BYTES:
        raise EvidenceImportFailed(
            f"Evidence bundle is {len(document)} bytes; this build parses at most "
            f"{MAX_PARSE_BYTES}. The {MAX_IMPORT_BYTES}-byte figure in ADR-0026 §3 is the "
            "transfer cap, not the parse cap.",
            details={
                "size_bytes": len(document),
                "max_parse_bytes": MAX_PARSE_BYTES,
                "max_transfer_bytes": MAX_IMPORT_BYTES,
            },
        )
    try:
        return load_envelope(
            document,
            expect=BUNDLE_SCHEMA,
            supported=_accepted_versions(accept_schema_majors),
            max_bytes=MAX_PARSE_BYTES,
        )
    except setspec.SchemaVersionUnsupported as exc:
        details = dict(exc.details or {})
        raise EvidenceSchemaVersionUnsupported(
            f"{exc} This build accepts major version(s) "
            f"{', '.join(str(major) for major in accept_schema_majors)}; no evidence was "
            "changed.",
            details={
                **details,
                "schema": BUNDLE_SCHEMA,
                "accepted_majors": list(accept_schema_majors),
            },
        ) from exc
    except setspec.ValidationError as exc:
        raise EvidenceImportFailed(str(exc), details=dict(exc.details or {})) from exc


def _claimed(raw: object, *path: str) -> str | None:
    """Read a string a rejected record claimed, without trusting its shape."""
    node: object = raw
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) else None


def _first_error(exc: PydanticValidationError) -> str:
    """Summarize a pydantic failure as one sentence naming every offending field path."""
    paths = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
    joined = ", ".join(paths[:5])
    suffix = "" if len(paths) <= 5 else f" (and {len(paths) - 5} more)"
    return f"record failed {BUNDLE_SCHEMA} validation at: {joined}{suffix}"


def _write(  # noqa: PLR0913 — one transaction with every import input threaded through
    database: Database,
    *,
    validated: list[tuple[int, CapabilityEvidenceFields]],
    rejected: list[RejectedRecord],
    source_key: str,
    source_kind: str,
    url: str | None,
    complete: bool,
    total: int,
    schema_version: str,
    generated_at: datetime | None,
    now: datetime,
    current_environment: Mapping[str, Any] | None,
) -> ImportOutcome:
    """Persist a validated bundle in one unit of work."""
    imported = updated = unmatched = ambiguous = bound = upgraded = superseded = 0
    seen_keys: set[tuple[str, ...]] = set()

    with database.write() as session:
        source = _upsert_source(
            session,
            source_key=source_key,
            kind=source_kind,
            url=url,
            schema_version=schema_version,
            generated_at=generated_at,
            now=now,
        )
        source_row_id = source.id
        registry = _registry_of(session)
        existing_keys = {
            (
                source_row_id,
                row.canonical_id,
                row.runtime_profile_hash,
                row.machine_fingerprint,
                row.capability_id,
                row.policy_version,
            )
            for row in session.query(CapabilityEvidence).filter_by(source_id=source_row_id).all()
        }

        for index, record in validated:
            key = _uniqueness_key(record, source_row_id)
            if key in seen_keys:
                rejected.append(
                    RejectedRecord(
                        index=index,
                        canonical_id=record.model.canonical_id,
                        capability_id=record.capability_id,
                        reason="DUPLICATE_RECORD",
                        detail=(
                            "a second record in this bundle carries the same "
                            "(canonical_id, runtime_profile_hash, machine_fingerprint, "
                            "capability_id, policy_version); two measurements are not merged "
                            "into one row"
                        ),
                    )
                )
                continue
            seen_keys.add(key)

            binding = bind_identity(_identity_of(record), registry)
            if binding.upgrade_model_id is not None and binding.upgrade_digest is not None:
                row = session.get(Model, binding.upgrade_model_id)
                if row is not None and row.artifact_digest is None:
                    row.artifact_digest = binding.upgrade_digest
                    row.canonical_id = record.model.canonical_id
                    row.identity_confidence = "digest"
                    upgraded += 1
                    registry = _registry_of(session)

            values = _row_values(
                record,
                binding_state=binding.match_state,
                model_id=binding.model_id,
                source_row_id=source_row_id,
                now=now,
                current_environment=current_environment,
            )
            upsert(
                session,
                CapabilityEvidence,
                values,
                index_elements=[
                    "source_id",
                    "canonical_id",
                    "runtime_profile_hash",
                    "machine_fingerprint",
                    "capability_id",
                    "policy_version",
                ],
            )
            if key in existing_keys:
                updated += 1
            else:
                imported += 1
            if binding.match_state == "bound":
                bound += 1
            elif binding.match_state == "unmatched":
                unmatched += 1
            else:
                ambiguous += 1

        if complete:
            for key in existing_keys - seen_keys:
                stale_row = (
                    session.query(CapabilityEvidence)
                    .filter_by(
                        source_id=key[0],
                        canonical_id=key[1],
                        runtime_profile_hash=key[2],
                        machine_fingerprint=key[3],
                        capability_id=key[4],
                        policy_version=key[5],
                    )
                    .one_or_none()
                )
                if stale_row is not None and stale_row.stale_reason != "superseded":
                    stale_row.stale = True
                    stale_row.stale_reason = "superseded"
                    superseded += 1

        source.record_count = imported + updated
        session.flush()

    return ImportOutcome(
        source_row_id=source_row_id,
        source_key=source_key,
        schema_version=schema_version,
        generated_at=generated_at,
        complete=complete,
        total=total,
        imported=imported,
        updated=updated,
        unmatched=unmatched,
        ambiguous=ambiguous,
        bound=bound,
        upgraded_models=upgraded,
        superseded=superseded,
        rejected=tuple(rejected),
    )


def _row_values(  # noqa: PLR0913 — one row, every column named
    record: CapabilityEvidenceFields,
    *,
    binding_state: str,
    model_id: str | None,
    source_row_id: str,
    now: datetime,
    current_environment: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Render one validated record as the column values ``capability_evidence`` stores."""
    dumped = record.model_dump(mode="json")
    environment = _mapping_or_none(dumped.get("environment"))
    drift = environment_drift(environment, current_environment, capability_id=record.capability_id)
    staleness = evaluate_staleness(
        measured_at=record.measured_at,
        now=now,
        capability_id=record.capability_id,
        drift_field=drift,
    )
    dispersion = record.dispersion
    return {
        "model_id": model_id,
        "provider_kind": record.model.provider_kind,
        "provider_model_name": record.model.provider_model_name,
        "artifact_digest": record.model.artifact_digest,
        "canonical_id": record.model.canonical_id,
        "match_state": binding_state,
        "runtime_profile_hash": record.runtime_profile_hash,
        "machine_fingerprint": record.machine_fingerprint,
        "capability_id": record.capability_id,
        "score": record.score,
        "confidence": record.confidence,
        "sample_count": record.sample_count,
        "excluded_count": record.excluded_count,
        "dispersion": float(dispersion) if is_supported(dispersion) else None,
        "dispersion_unavailable_reason": (
            None if is_supported(dispersion) else "reported unsupported by the producer"
        ),
        "benchmark_versions_json": _mapping_or_none(dumped.get("benchmark_versions")),
        "dataset_hashes_json": _mapping_or_none(dumped.get("dataset_hashes")),
        "prompt_subset_hashes_json": _mapping_or_none(dumped.get("prompt_subset_hashes")),
        "contributing_metrics_json": list(dumped.get("contributing_metrics") or []) or None,
        "source_run_ids_json": list(dumped.get("source_run_ids") or []) or None,
        "identity_confidence": record.model.identity_confidence,
        "environment_snapshot_json": environment,
        "goal_json": _goal_json(record),
        "measured_at": record.measured_at,
        "computed_at": record.computed_at,
        "imported_at": now,
        "source_id": source_row_id,
        "policy_version": record.policy_version,
        "vocabulary_version": record.vocabulary_version,
        "stale": staleness.stale,
        "stale_reason": staleness.reason,
    }


def rebind_evidence_in(session: Session) -> RebindOutcome:
    """Re-evaluate every evidence row's ``match_state`` against the current registry.

    ADR-0022 §4 requires this on **every discovery pass**, which is what makes "imports
    successfully, is reported as unmatched, and binds automatically on the next discovery pass"
    true with no re-import. It runs inside the caller's transaction so that discovery and the
    binding it causes commit together.

    Args:
        session: An open write session — discovery's own.

    Returns:
        What changed.
    """
    registry = _registry_of(session)
    rows = session.query(CapabilityEvidence).all()
    bound = unbound = upgraded = 0
    for row in rows:
        identity = EvidenceIdentity(
            provider_kind=row.provider_kind,
            provider_model_name=row.provider_model_name,
            artifact_digest=row.artifact_digest,
            canonical_id=row.canonical_id,
        )
        binding = bind_identity(identity, registry)
        if binding.upgrade_model_id is not None and binding.upgrade_digest is not None:
            model = session.get(Model, binding.upgrade_model_id)
            if model is not None and model.artifact_digest is None:
                model.artifact_digest = binding.upgrade_digest
                model.canonical_id = row.canonical_id
                model.identity_confidence = "digest"
                upgraded += 1
                registry = _registry_of(session)
        was_bound = row.match_state == "bound"
        if row.match_state != binding.match_state or row.model_id != binding.model_id:
            row.match_state = binding.match_state
            row.model_id = binding.model_id
            if binding.match_state == "bound":
                bound += 1
            elif was_bound:
                unbound += 1
    session.flush()
    return RebindOutcome(bound=bound, unbound=unbound, upgraded_models=upgraded, examined=len(rows))


def rebind_evidence(database: Database) -> RebindOutcome:
    """Re-evaluate every evidence row's binding in a transaction of its own.

    Args:
        database: The application's database handle.

    Returns:
        What changed.
    """
    with database.write() as session:
        return rebind_evidence_in(session)
