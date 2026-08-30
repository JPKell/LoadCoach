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

from loadcoach.domain.authorization import Principal, authorize
from loadcoach.domain.evidence_policy import (
    CalibrationFacts,
    EvidenceCandidate,
    EvidenceIdentity,
    EvidenceOverview,
    LocalModel,
    bind_identity,
    environment_drift,
    evaluate_staleness,
)
from loadcoach.domain.routing.subject import CapabilitySignal
from loadcoach.infrastructure.db.models import CapabilityEvidence, EvidenceSource, Model
from loadcoach.infrastructure.freeweight_client import (
    MAX_IMPORT_BYTES,
    EvidenceSourceRefused,
    EvidenceSourceUnreachable,
    FreeWeightClient,
    policy_from_settings,
    resolve_credential,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from sqlalchemy.orm import Session

    from loadcoach.config import EvidenceSettings
    from loadcoach.services.database import Database

__all__ = [
    "BUNDLE_SCHEMA",
    "DEFAULT_EVIDENCE_LIMIT",
    "MAX_EVIDENCE_LIMIT",
    "MAX_PARSE_BYTES",
    "EvidenceImportFailed",
    "EvidenceSchemaVersionUnsupported",
    "CapabilityCoverage",
    "EvidencePage",
    "EvidenceQuery",
    "EvidenceRow",
    "ImportOutcome",
    "RejectedRecord",
    "RebindOutcome",
    "SourceStatus",
    "bound_signals_for_routing",
    "capability_coverage",
    "credential_for",
    "evidence_overview",
    "import_bundle",
    "last_generated_at",
    "list_sources",
    "query_evidence",
    "mark_source_unreachable",
    "rebind_evidence",
    "rebind_evidence_in",
    "refresh_from_freeweight",
    "source_for_url",
]

BUNDLE_SCHEMA: Final[str] = "benchmark.evidence_bundle"
"""The one payload type ``POST /evidence/import`` accepts."""

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


def source_for_url(session: Session, url: str) -> EvidenceSource | None:
    """Return the source row for ``url``, deterministically.

    ``evidence_sources.url`` is not unique, and cannot be: a failed refresh records a placeholder
    row before any bundle has named its producer, and two producers could in principle sit behind
    one reverse proxy. The row that has actually imported wins; among rows that have not, the
    oldest does, so the answer does not depend on insertion order.

    Args:
        session: An open session.
        url: The source URL.

    Returns:
        The row, or ``None``.
    """
    rows = session.query(EvidenceSource).filter_by(url=url).all()
    if not rows:
        return None
    imported = [row for row in rows if row.last_import_at is not None]
    if imported:
        return max(imported, key=lambda row: (row.last_import_at, row.id))
    return min(rows, key=lambda row: row.id)


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
    """Insert or refresh the ``evidence_sources`` row this bundle belongs to.

    A refresh that failed before anything was ever imported left a **placeholder** row keyed by
    the URL, because at that point nobody knew what the producer calls itself. The first
    successful import adopts that row rather than adding a second one beside it: two rows for one
    URL would leave a permanent "unreachable" source next to a working one, which is what the I4
    demonstration found.
    """
    existing = session.query(EvidenceSource).filter_by(source_key=source_key).one_or_none()
    if existing is None and url is not None:
        placeholder = session.query(EvidenceSource).filter_by(source_key=url, url=url).one_or_none()
        if placeholder is not None:
            placeholder.source_key = source_key
            existing = placeholder
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
    principal: Principal | None = None,
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
    authorize(principal, "admin")
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

        # A reachable source clears the badge that said it was not. `source_unreachable` is a
        # statement about the *source*, never about the measurement, so a successful import
        # retires it and the row falls back to whatever its own age says (ADR-0017's staleness
        # surface). Rows this bundle carried are recomputed below anyway.
        for badged in (
            session.query(CapabilityEvidence)
            .filter_by(source_id=source_row_id, stale_reason="source_unreachable")
            .all()
        ):
            refreshed = evaluate_staleness(
                measured_at=badged.measured_at, now=now, capability_id=badged.capability_id
            )
            badged.stale = refreshed.stale
            badged.stale_reason = refreshed.reason

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
        "record_json": dumped,
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


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """One configured or observed evidence source, as ``GET /evidence/sources`` reports it.

    Attributes:
        source_key: The producer's own ``source_id``, or ``"freeweight"`` for a configured source
            nothing has yet been imported from.
        kind: ``freeweight_api``, ``file`` or ``manual``.
        url: Where it was pulled from, when it was pulled.
        last_import_at: When LoadCoach last imported from it.
        last_status: ``ok``, ``unreachable``, ``refused`` or ``failed``.
        schema_version: The bundle version last seen.
        record_count: How many records that import carried.
        error_text: The last failure's message, cleared by a success.
        generated_at: The producer's own timestamp for that bundle — what the next ``?since=``
            sends back (ADR-0022 §5).
        rows: How many evidence rows this source currently owns.
        stale_rows: How many of them are marked stale.
        newest_measured_at: The freshest measurement this source supplied.
        configured: Whether ``[evidence] freeweight_url`` names this source.
    """

    source_key: str
    kind: str
    url: str | None
    last_import_at: datetime | None
    last_status: str | None
    schema_version: str | None
    record_count: int
    error_text: str | None
    generated_at: datetime | None
    rows: int
    stale_rows: int
    newest_measured_at: datetime | None
    configured: bool = False

    def as_json(self) -> dict[str, Any]:
        """Return this source as the API reports it."""
        from baseaicore.timeutil import to_rfc3339

        def when(value: datetime | None) -> str | None:
            return None if value is None else to_rfc3339(value)

        return {
            "source_id": self.source_key,
            "kind": self.kind,
            "url": self.url,
            "last_import_at": when(self.last_import_at),
            "last_status": self.last_status,
            "schema_version": self.schema_version,
            "record_count": self.record_count,
            "error_text": self.error_text,
            "generated_at": when(self.generated_at),
            "rows": self.rows,
            "stale_rows": self.stale_rows,
            "newest_measured_at": when(self.newest_measured_at),
            "configured": self.configured,
        }


def list_sources(database: Database, *, configured_url: str = "") -> tuple[SourceStatus, ...]:
    """Return every evidence source, with the row counts that make its status meaningful.

    Args:
        database: The application's database handle.
        configured_url: ``[evidence] freeweight_url``. An empty string means **not configured**,
            which is a different state from unavailable and is reported as such.

    Returns:
        One :class:`SourceStatus` per row in ``evidence_sources``, ordered by key.
    """
    from sqlalchemy import func, select

    with database.read() as session:
        sources = session.query(EvidenceSource).order_by(EvidenceSource.source_key).all()
        counts: dict[str, int] = {
            row[0]: int(row[1])
            for row in session.execute(
                select(CapabilityEvidence.source_id, func.count(CapabilityEvidence.id)).group_by(
                    CapabilityEvidence.source_id
                )
            ).all()
        }
        stale_counts: dict[str, int] = {
            row[0]: int(row[1])
            for row in session.execute(
                select(CapabilityEvidence.source_id, func.count(CapabilityEvidence.id))
                .where(CapabilityEvidence.stale.is_(True))
                .group_by(CapabilityEvidence.source_id)
            ).all()
        }
        newest: dict[str, object] = {
            row[0]: row[1]
            for row in session.execute(
                select(
                    CapabilityEvidence.source_id, func.max(CapabilityEvidence.measured_at)
                ).group_by(CapabilityEvidence.source_id)
            ).all()
        }
        return tuple(
            SourceStatus(
                source_key=row.source_key,
                kind=row.kind,
                url=row.url,
                last_import_at=row.last_import_at,
                last_status=row.last_status,
                schema_version=row.schema_version,
                record_count=row.record_count,
                error_text=row.error_text,
                generated_at=row.generated_at,
                rows=int(counts.get(row.id, 0)),
                stale_rows=int(stale_counts.get(row.id, 0)),
                newest_measured_at=_as_datetime(newest.get(row.id)),
                configured=bool(configured_url) and row.url == configured_url,
            )
            for row in sources
        )


def _as_datetime(value: object) -> datetime | None:
    """Coerce a ``MAX()`` result, which SQLite returns as text, to a datetime."""
    from datetime import UTC
    from datetime import datetime as _datetime

    if value is None:
        return None
    if isinstance(value, _datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = _datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def mark_source_unreachable(database: Database, *, url: str, reason: str, now: datetime) -> int:
    """Record that a source could not be reached, and badge the evidence it supplied.

    The degradation contract, made durable: the last import is **retained**, its rows are marked
    stale with the reason ``source_unreachable``, and routing goes on using them and its priors.
    Nothing is deleted, because an unreachable FreeWeight has said nothing about whether its
    measurements are still true.

    Args:
        database: The application's database handle.
        url: The source URL that failed.
        reason: The failure, for ``error_text``.
        now: The instant to record.

    Returns:
        How many evidence rows were newly badged.
    """
    badged = 0
    with database.write() as session:
        source = source_for_url(session, url)
        if source is None:
            return 0
        source.last_status = "unreachable"
        source.error_text = reason
        source.last_import_at = source.last_import_at
        for row in session.query(CapabilityEvidence).filter_by(source_id=source.id).all():
            if row.stale_reason == "superseded":
                continue
            if not row.stale or row.stale_reason != "source_unreachable":
                badged += 1
            row.stale = True
            row.stale_reason = "source_unreachable"
        session.flush()
    return badged


def last_generated_at(database: Database, *, url: str) -> datetime | None:
    """Return the producer's ``generated_at`` for the last bundle pulled from ``url``.

    ADR-0022 §5: *a client never supplies its own clock.* It stores the ``generated_at`` of the
    previous bundle envelope and sends that value back as ``?since=``, which makes the comparison
    single-clock and correct across machines.

    Args:
        database: The application's database handle.
        url: The source URL.

    Returns:
        The stored timestamp, or ``None`` when this source has never been imported from — in
        which case ADR-0022 §5 requires a **complete** pull.
    """
    with database.read() as session:
        source = source_for_url(session, url)
        return None if source is None else source.generated_at


def refresh_from_freeweight(
    database: Database,
    settings: EvidenceSettings,
    *,
    now: datetime,
    client: FreeWeightClient | None = None,
    current_environment: Mapping[str, Any] | None = None,
) -> ImportOutcome | None:
    """Pull from the configured FreeWeight and import what comes back.

    Degradation is the point of this function, not an afterthought:

    * ``freeweight_url = ""`` is **not configured**. Nothing is attempted and ``None`` is
      returned — a different state from unavailable, and the two must not be conflated in the UI
      or in ``/health``.
    * A source that refuses or cannot be reached leaves the previous import in place, badges its
      rows ``source_unreachable``, and returns ``None``. Routing continues on that evidence and
      on its priors, and says so.

    Args:
        database: The application's database handle.
        settings: The ``[evidence]`` block.
        now: The refresh instant.
        client: A client to use. Built from ``settings`` when not supplied.
        current_environment: This machine's provider/driver facts, for drift detection.

    Returns:
        The :class:`ImportOutcome`, or ``None`` when the source is unconfigured or unavailable.
    """
    url = settings.freeweight_url.strip()
    if not url:
        return None

    owned = client is None
    fetch_client = (
        client if client is not None else FreeWeightClient(policy_from_settings(settings))
    )
    try:
        credential = credential_for(settings, url)
        fetched = fetch_client.fetch(
            url, since=last_generated_at(database, url=url), credential=credential
        )
    except EvidenceSourceRefused as exc:
        _record_source_failure(database, url=url, status="refused", reason=str(exc), now=now)
        return None
    except EvidenceSourceUnreachable as exc:
        mark_source_unreachable(database, url=url, reason=str(exc), now=now)
        _record_source_failure(database, url=url, status="unreachable", reason=str(exc), now=now)
        return None
    finally:
        if owned:
            fetch_client.close()

    try:
        return import_bundle(
            database,
            fetched.document,
            now=now,
            accept_schema_majors=settings.accept_schema_majors,
            source_kind="freeweight_api",
            url=url,
            current_environment=current_environment,
        )
    except SuiteError as exc:
        _record_source_failure(database, url=url, status="failed", reason=str(exc), now=now)
        raise


def credential_for(settings: EvidenceSettings, url: str) -> str | None:
    """Return the bearer token for ``url``, or ``None`` when it belongs to another host.

    Spec §14: *a credential configured for one evidence source is never sent to any other host.*
    The configured credential belongs to ``evidence.freeweight_url``'s origin; an ad-hoc import
    from a different allowlisted host is unauthenticated rather than credentialed with someone
    else's token.

    Args:
        settings: The ``[evidence]`` block.
        url: The URL about to be fetched.

    Returns:
        The token, or ``None``.
    """
    import httpx

    configured = settings.freeweight_url.strip()
    if not configured:
        return None
    try:
        target, owner = httpx.URL(url), httpx.URL(configured)
    except (httpx.InvalidURL, ValueError):
        return None
    if (target.scheme, target.host, target.port) != (owner.scheme, owner.host, owner.port):
        return None
    return resolve_credential(settings)


def _record_source_failure(
    database: Database, *, url: str, status: str, reason: str, now: datetime
) -> None:
    """Record a failed refresh on the source row, creating it if this is the first attempt."""
    with database.write() as session:
        source = source_for_url(session, url)
        if source is None:
            source = EvidenceSource(
                source_key=url,
                kind="freeweight_api",
                url=url,
                record_count=0,
                created_at=now,
            )
            session.add(source)
        source.last_status = status
        source.error_text = reason
        session.flush()


def evidence_overview(database: Database, *, configured_url: str = "") -> EvidenceOverview:
    """Summarize the evidence store for the explanation, the UI and ``/health``.

    Args:
        database: The application's database handle.
        configured_url: ``[evidence] freeweight_url``.

    Returns:
        The :class:`EvidenceOverview`. On a fresh install with nothing configured this reports
        ``not_configured``, which is the state spec §6's degradation contract names.
    """
    from sqlalchemy import func, select

    from loadcoach.domain.evidence_policy import policy_version_key

    with database.read() as session:
        totals = session.execute(
            select(
                func.count(CapabilityEvidence.id),
                func.sum(_case_one(CapabilityEvidence.match_state == "bound")),
                func.sum(_case_one(CapabilityEvidence.match_state == "unmatched")),
                func.sum(_case_one(CapabilityEvidence.match_state == "ambiguous_name_only")),
                func.sum(_case_one(CapabilityEvidence.stale.is_(True))),
                func.min(CapabilityEvidence.measured_at),
                func.max(CapabilityEvidence.measured_at),
                func.max(CapabilityEvidence.imported_at),
            )
        ).one()
        policies = [
            str(row[0])
            for row in session.execute(select(CapabilityEvidence.policy_version).distinct()).all()
        ]
        vocabularies = [
            str(row[0])
            for row in session.execute(
                select(CapabilityEvidence.vocabulary_version).distinct()
            ).all()
        ]
        source = None
        if configured_url:
            source = source_for_url(session, configured_url)
        if source is None:
            source = (
                session.query(EvidenceSource).order_by(EvidenceSource.last_import_at.desc()).first()
            )
        return EvidenceOverview(
            configured=bool(configured_url),
            source_status=None if source is None else source.last_status,
            rows=int(totals[0] or 0),
            bound=int(totals[1] or 0),
            unmatched=int(totals[2] or 0),
            ambiguous=int(totals[3] or 0),
            stale=int(totals[4] or 0),
            imported_at=_as_datetime(totals[7]),
            generated_at=None if source is None else source.generated_at,
            oldest_measured_at=_as_datetime(totals[5]),
            newest_measured_at=_as_datetime(totals[6]),
            bundle_schema_version=None if source is None else source.schema_version,
            policy_version=max(policies, key=policy_version_key, default=None),
            vocabulary_version=max(vocabularies, key=policy_version_key, default=None),
            error_text=None if source is None else source.error_text,
        )


def _case_one(condition: Any) -> Any:  # noqa: ANN401 — a SQLAlchemy boolean expression
    """Return ``1`` where ``condition`` holds and ``0`` elsewhere, for a dialect-neutral count."""
    from sqlalchemy import case

    return case((condition, 1), else_=0)


def bound_signals_for_routing(
    database: Database,
    *,
    weights: Mapping[str, float],
    now: datetime,
    local_machine_fingerprint: str | None = None,
) -> dict[str, tuple[CapabilitySignal, ...]]:
    """Read the benchmark evidence routing may score, keyed by model.

    Three rules are applied here rather than in scoring, because each needs either the store or
    the clock and scoring has neither:

    * **Only ``match_state = 'bound'``.** The query filters on it and uses
      ``(model_id, capability_id)``, which is data model §4's stated plan for exactly this
      lookup. Unbound rows are counted in the explanation's summary instead.
    * **The ``user.*`` opt-in.** A ``user.*`` capability the active task profile does not name
      never becomes a signal at all, so importing one changes no existing decision — not its
      score, not its flags, not its breakdown (ADR-0032 §6). Naming it in the profile makes it
      score on the next decision with no re-import.
    * **One record per (capability, runtime profile).** Several rows can exist for one subject
      under different policy versions or machines;
      :func:`~loadcoach.domain.evidence_policy.collapse_evidence` selects one rather than
      averaging, and rows measured under a *different* profile survive so the explanation can
      name the mismatch with a hash that evidence actually exists for.

    Args:
        database: The application's database handle.
        weights: The active task profile's capability weights — the ``user.*`` gate's input.
        now: The instant ages are measured from.
        local_machine_fingerprint: This machine's fingerprint, or ``None``.

    Returns:
        ``model_id -> signals``. A model with no bound evidence has no entry, not an empty one.
    """
    from sqlalchemy import select

    from loadcoach.domain.evidence_policy import collapse_evidence, weights_admit

    with database.read() as session:
        rows = (
            session.execute(
                select(CapabilityEvidence).where(CapabilityEvidence.match_state == "bound")
            )
            .scalars()
            .all()
        )

    per_model: dict[str, list[EvidenceCandidate]] = {}
    facts: dict[str, CapabilityEvidence] = {}
    for row in rows:
        if row.model_id is None or not weights_admit(row.capability_id, weights):
            continue
        facts[row.id] = row
        per_model.setdefault(row.model_id, []).append(
            EvidenceCandidate(
                row_id=row.id,
                capability_id=row.capability_id,
                runtime_profile_hash=row.runtime_profile_hash,
                machine_fingerprint=row.machine_fingerprint,
                policy_version=row.policy_version,
                measured_at=row.measured_at,
                score=row.score,
                confidence=row.confidence,
                sample_count=row.sample_count,
                benchmark_versions=_versions_of(row.benchmark_versions_json),
                calibration=_calibration_of(row.goal_json),
            )
        )

    signals: dict[str, tuple[CapabilitySignal, ...]] = {}
    for model_id, candidates in per_model.items():
        selected = collapse_evidence(
            candidates, local_machine_fingerprint=local_machine_fingerprint
        )
        signals[model_id] = tuple(
            CapabilitySignal(
                capability_id=candidate.capability_id,
                source="benchmark",
                score=candidate.score,
                confidence=candidate.confidence,
                runtime_profile_hash=candidate.runtime_profile_hash,
                machine_fingerprint=candidate.machine_fingerprint,
                measured_at=candidate.measured_at,
                sample_count=candidate.sample_count,
                match_state="bound",
                age_days=max(0, int((now - candidate.measured_at).total_seconds() // 86400)),
                stale=bool(facts[candidate.row_id].stale),
                stale_reason=facts[candidate.row_id].stale_reason,
                calibration=candidate.calibration,
            )
            for candidate in selected
        )
    return signals


def _versions_of(value: object) -> tuple[tuple[str, str], ...]:
    """Render a stored ``benchmark_versions`` mapping as sorted pairs."""
    if not isinstance(value, dict):
        return ()
    return tuple(sorted((str(key), str(item)) for key, item in value.items()))


def _calibration_of(value: object) -> CalibrationFacts | None:
    """Lift a stored goal group's calibration into the domain's value object."""
    if not isinstance(value, dict):
        return None
    calibration = value.get("calibration")
    if not isinstance(calibration, dict):
        return None
    measured_at = _as_datetime(calibration.get("measured_at"))
    if measured_at is None:
        return None
    try:
        return CalibrationFacts(
            kappa_w=float(calibration["kappa_w"]),
            n_holdout=int(calibration["n_holdout"]),
            graded_by=str(calibration["graded_by"]),
            measured_at=measured_at,
        )
    except (KeyError, TypeError, ValueError):
        return None


DEFAULT_EVIDENCE_LIMIT: Final[int] = 50
"""Page size for ``GET /evidence`` when the caller does not say."""

MAX_EVIDENCE_LIMIT: Final[int] = 500
"""The largest page ``GET /evidence`` will produce, so one request cannot ask for the whole
table."""


@dataclass(frozen=True, slots=True)
class EvidenceQuery:
    """``GET /evidence``'s filters (api.md §7).

    Attributes:
        capability: Exact capability ID.
        model: Canonical ID.
        match_state: ``bound``, ``unmatched`` or ``ambiguous_name_only``.
        min_confidence: Records at or above this confidence.
        stale: ``True`` for stale records only, ``False`` for fresh only, ``None`` for both.
        limit: Page size.
        cursor: The previous page's ``next_cursor`` — the last row's ULID.
    """

    capability: str | None = None
    model: str | None = None
    match_state: str | None = None
    min_confidence: float | None = None
    stale: bool | None = None
    limit: int = DEFAULT_EVIDENCE_LIMIT
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    """One stored record, in the shape the page and the API both read.

    Attributes:
        row_id: The ``capability_evidence`` ULID; also the pagination cursor.
        canonical_id: The measured model.
        capability_id: The capability.
        match_state: Whether it scores.
        score: The measured ability.
        confidence: FreeWeight's number, applied never recomputed.
        sample_count: Samples behind the score.
        excluded_count: Samples excluded, visibly.
        runtime_profile_hash: The profile it was measured under.
        machine_fingerprint: The machine it was measured on.
        measured_at: What freshness decays from.
        imported_at: When it arrived here.
        age_days: Age at the moment of the query.
        stale: Its staleness badge.
        stale_reason: Which of the four reasons raised it.
        policy_version: The confidence policy it was computed under.
        source_key: Which producer supplied it.
        goal: The ADR-0032 goal group, on a ``user.*`` record.
        record: The ``capability.evidence`` payload as it arrived, for the SetSpec envelope.
    """

    row_id: str
    canonical_id: str
    capability_id: str
    match_state: str
    score: float
    confidence: float
    sample_count: int
    excluded_count: int
    runtime_profile_hash: str
    machine_fingerprint: str
    measured_at: datetime
    imported_at: datetime
    age_days: int
    stale: bool
    stale_reason: str | None
    policy_version: str
    source_key: str
    goal: dict[str, Any] | None
    record: dict[str, Any] | None

    def as_envelope(self, *, generator_version: str) -> str:
        """Render this row as a ``capability.evidence`` SetSpec envelope (ADR-0025 §2).

        The payload is the producer's own document, re-emitted unchanged; the envelope's
        ``generator`` names **LoadCoach**, because ADR-0025 §3 makes the generator the
        application doing the writing and this response is LoadCoach's.

        Args:
            generator_version: LoadCoach's version.

        Returns:
            Canonical JSON for one envelope.
        """
        from setspec import GeneratorInfo, SchemaVersion, dump_envelope

        return dump_envelope(
            self.record or {},
            schema="capability.evidence",
            version=SchemaVersion(1, 0),
            generator=GeneratorInfo(name="loadcoach", version=generator_version),
            generated_at=self.imported_at,
        )


@dataclass(frozen=True, slots=True)
class EvidencePage:
    """One page of evidence rows, with the cursor for the next."""

    items: tuple[EvidenceRow, ...]
    limit: int
    next_cursor: str | None
    has_more: bool
    total: int


def query_evidence(database: Database, query: EvidenceQuery, *, now: datetime) -> EvidencePage:
    """Read imported evidence, filtered and paged (api.md §7).

    Args:
        database: The application's database handle.
        query: The filters and page window.
        now: The instant ages are measured from.

    Returns:
        The :class:`EvidencePage`.
    """
    from sqlalchemy import func, select

    limit = max(1, min(query.limit, MAX_EVIDENCE_LIMIT))
    statement = select(CapabilityEvidence)
    if query.capability:
        statement = statement.where(CapabilityEvidence.capability_id == query.capability)
    if query.model:
        statement = statement.where(CapabilityEvidence.canonical_id == query.model)
    if query.match_state:
        statement = statement.where(CapabilityEvidence.match_state == query.match_state)
    if query.min_confidence is not None:
        statement = statement.where(CapabilityEvidence.confidence >= query.min_confidence)
    if query.stale is not None:
        statement = statement.where(CapabilityEvidence.stale.is_(query.stale))

    with database.read() as session:
        total = int(
            session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
        )
        paged = statement
        if query.cursor:
            paged = paged.where(CapabilityEvidence.id > query.cursor)
        rows = (
            session.execute(paged.order_by(CapabilityEvidence.id).limit(limit + 1)).scalars().all()
        )
        sources = {row.id: row.source_key for row in session.query(EvidenceSource).all()}
        has_more = len(rows) > limit
        window = rows[:limit]
        items = tuple(
            EvidenceRow(
                row_id=row.id,
                canonical_id=row.canonical_id,
                capability_id=row.capability_id,
                match_state=row.match_state,
                score=row.score,
                confidence=row.confidence,
                sample_count=row.sample_count,
                excluded_count=row.excluded_count,
                runtime_profile_hash=row.runtime_profile_hash,
                machine_fingerprint=row.machine_fingerprint,
                measured_at=row.measured_at,
                imported_at=row.imported_at,
                age_days=max(0, int((now - row.measured_at).total_seconds() // 86400)),
                stale=bool(row.stale),
                stale_reason=row.stale_reason,
                policy_version=row.policy_version,
                source_key=sources.get(row.source_id, row.source_id),
                goal=row.goal_json if isinstance(row.goal_json, dict) else None,
                record=row.record_json if isinstance(row.record_json, dict) else None,
            )
            for row in window
        )
    return EvidencePage(
        items=items,
        limit=limit,
        next_cursor=items[-1].row_id if items and has_more else None,
        has_more=has_more,
        total=total,
    )


@dataclass(frozen=True, slots=True)
class CapabilityCoverage:
    """How well one capability is covered by imported evidence (dev-plan P6's UI requirement).

    Attributes:
        capability_id: The capability.
        models: How many distinct models carry evidence for it.
        bound: How many of those records score.
        stale: How many carry a staleness badge.
        best_score: The highest score among **bound** records — the one routing could use.
        best_confidence: That record's confidence.
        newest_measured_at: The freshest measurement for this capability.
        oldest_measured_at: The oldest.
    """

    capability_id: str
    models: int
    bound: int
    stale: int
    best_score: float | None
    best_confidence: float | None
    newest_measured_at: datetime | None
    oldest_measured_at: datetime | None


def capability_coverage(database: Database) -> tuple[CapabilityCoverage, ...]:
    """Summarize evidence coverage per capability, for the Benchmarks page.

    Args:
        database: The application's database handle.

    Returns:
        One row per capability with evidence, ordered by capability ID. A capability with no
        evidence has **no row**: an empty coverage table is an honest "nothing measured", and a
        row of zeroes would read as "measured at zero".
    """
    with database.read() as session:
        rows = session.query(CapabilityEvidence).all()
    grouped: dict[str, list[CapabilityEvidence]] = {}
    for row in rows:
        grouped.setdefault(row.capability_id, []).append(row)
    coverage: list[CapabilityCoverage] = []
    for capability_id, group in sorted(grouped.items()):
        bound = [row for row in group if row.match_state == "bound"]
        best = max(bound, key=lambda row: row.score, default=None)
        coverage.append(
            CapabilityCoverage(
                capability_id=capability_id,
                models=len({row.canonical_id for row in group}),
                bound=len(bound),
                stale=sum(1 for row in group if row.stale),
                best_score=None if best is None else best.score,
                best_confidence=None if best is None else best.confidence,
                newest_measured_at=max(row.measured_at for row in group),
                oldest_measured_at=min(row.measured_at for row in group),
            )
        )
    return tuple(coverage)
