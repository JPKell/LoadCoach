"""loadcoach.services.models — discovery through ModelRack and the registry read path.

Unavailable models are flagged with a reason, never deleted (dev-plan P2 test list) — history of
what a machine has seen outlives whether it can see it right now, the same principle database
standards §8 states for results.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from baseaicore import ValidationError, is_supported
from modelrack import ProviderError
from weightsdb import upsert

from loadcoach.domain.authorization import Principal, authorize
from loadcoach.domain.registry import (
    declared_capabilities_for,
    descriptor_geometry,
    validate_manual_score,
)
from loadcoach.infrastructure.db.models import Model, ModelCapability
from loadcoach.services.evidence import rebind_evidence_in

if TYPE_CHECKING:
    from baseaicore import ModelDescriptor
    from modelrack.provider import Provider
    from sqlalchemy.orm import Session

    from loadcoach.infrastructure.providers.factory import ProviderRegistration
    from loadcoach.services.database import Database

__all__ = [
    "ModelOverview",
    "registry_overview",
    "DEFAULT_MANUAL_SCORES_PATH",
    "DiscoveryOutcome",
    "RegistryEntry",
    "discover_models",
    "set_model_enabled",
    "import_manual_capability_scores",
    "list_registry",
    "try_discover_models",
]

DEFAULT_MANUAL_SCORES_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "manual_capability_scores.toml"
)


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    """The result of one :func:`discover_models` pass, summed over every registration.

    Attributes:
        added: Rows this pass created.
        updated: Rows this pass refreshed.
        unavailable: Rows this pass retired, having not been reported by a registration that
            answered.
        total: Descriptors read, across every registration that answered.
        checked_at: When.
        unreachable: The names of registrations that could not be listed. Empty on a clean pass;
            a name here means its models were left exactly as they were.
    """

    added: int
    updated: int
    unavailable: int
    total: int
    checked_at: datetime
    unreachable: tuple[str, ...] = ()


def _as_registrations(
    providers: Provider | Sequence[ProviderRegistration],
) -> tuple[ProviderRegistration, ...]:
    """Accept either a registration sequence or one bare provider.

    The bare form keeps every caller that holds a single handle — the CLI's one-shot commands,
    a test — working unchanged. Such a provider has no registration name, and the empty string is
    what "not recorded" already means on the column (migration 0008).
    """
    from loadcoach.infrastructure.providers.factory import ProviderRegistration

    if isinstance(providers, Sequence):
        return tuple(providers)
    return (
        ProviderRegistration(
            name="",
            kind=providers.kind.value,
            is_remote=False,
            provider=providers,
        ),
    )


def _upsert_model(
    session: Session,
    descriptor: ModelDescriptor,
    *,
    now: datetime,
    provider_name: str,
    is_remote: bool,
) -> Model:
    """Insert or update ``descriptor``'s identity row, upgrading a name-only sibling in place.

    Mirrors FreeWeight's own identity resolution (its ``ModelRepository.upsert_identity``): the
    natural key ``(provider_kind, provider_model_name, artifact_digest)`` has a partial unique
    index for the ``artifact_digest IS NULL`` case (data model §2, ``uq_models_name_only``), which
    ``weightsdb.upsert()`` cannot target (it does not support partial indexes — its own docstring
    names this exact case). A digest-confirmed sighting of a model LoadCoach previously only knew
    by name upgrades that row's identity rather than creating a second, duplicate one.

    ``provider_name`` and ``is_remote`` record the **registration** that served this sighting
    (ADR-0055). Identity is unchanged by them — ``provider_kind`` is part of it and the name is not
    (ADR-0008) — so two registrations of one kind serving the same weights are one row, and the
    name is the one the most recent pass saw it on.
    """
    identity = descriptor.identity
    provider_kind = identity.provider_kind.value

    existing = (
        session.query(Model)
        .filter_by(
            provider_kind=provider_kind,
            provider_model_name=identity.provider_model_name,
            artifact_digest=identity.artifact_digest,
        )
        .one_or_none()
    )
    if existing is None and identity.artifact_digest is not None:
        existing = (
            session.query(Model)
            .filter_by(
                provider_kind=provider_kind,
                provider_model_name=identity.provider_model_name,
                artifact_digest=None,
            )
            .one_or_none()
        )

    max_context = int(descriptor.max_context) if is_supported(descriptor.max_context) else None
    size_bytes = int(descriptor.size_bytes) if is_supported(descriptor.size_bytes) else None
    parameter_count = (
        int(descriptor.parameter_count) if is_supported(descriptor.parameter_count) else None
    )

    geometry = descriptor_geometry(descriptor)

    if existing is None:
        model = Model(
            descriptor_json=geometry,
            provider_kind=provider_kind,
            provider_model_name=identity.provider_model_name,
            artifact_digest=identity.artifact_digest,
            canonical_id=identity.canonical_id,
            identity_confidence=identity.identity_confidence.value,
            provider_name=provider_name,
            is_remote=is_remote,
            max_context=max_context,
            size_bytes=size_bytes,
            quantization=descriptor.quantization,
            family=descriptor.family,
            parameter_count=parameter_count,
            first_seen_at=now,
            last_seen_at=now,
            available=True,
            unavailable_reason=None,
        )
        session.add(model)
        session.flush()
        return model

    existing.descriptor_json = geometry
    existing.artifact_digest = identity.artifact_digest
    existing.canonical_id = identity.canonical_id
    existing.identity_confidence = identity.identity_confidence.value
    existing.provider_name = provider_name
    existing.is_remote = is_remote
    existing.max_context = max_context
    existing.size_bytes = size_bytes
    existing.quantization = descriptor.quantization
    existing.family = descriptor.family
    existing.parameter_count = parameter_count
    existing.last_seen_at = now
    existing.available = True
    existing.unavailable_reason = None
    session.flush()
    return existing


def _sync_declared_capabilities(
    session: Session, model: Model, descriptor: ModelDescriptor, *, now: datetime
) -> None:
    for declared in declared_capabilities_for(descriptor):
        upsert(
            session,
            ModelCapability,
            {
                "model_id": model.id,
                "capability_id": declared.capability_id,
                "score": declared.score,
                "confidence": declared.confidence,
                "source": "declared",
                "updated_at": now,
            },
            index_elements=["model_id", "capability_id", "source"],
        )


def discover_models(
    database: Database,
    providers: Provider | Sequence[ProviderRegistration],
    *,
    now: datetime,
    principal: Principal | None = None,
) -> DiscoveryOutcome:
    """Run one discovery pass over every registered provider and persist what they serve.

    A model previously discovered but absent from this pass is marked ``available=False`` with a
    reason — never deleted (dev-plan P2 test list, database standards §8). "Absent from this pass"
    means absent from a registration that **answered**: a registration that could not be listed
    takes no model out of the registry, because an unreachable provider is an availability fact
    about the process and not a statement that its models are gone (ADR-0067 rule 2, routing §4's
    ``model_unavailable``).

    Imported evidence is re-bound in the same transaction (ADR-0022 §4), so a bundle that arrived
    before its models were discovered starts scoring on this pass rather than on a re-import.

    Args:
        database: The application's database handle.
        providers: The registrations to discover through (ADR-0055), or a bare
            :class:`~modelrack.provider.Provider` for a caller that holds only one — it is
            discovered under the empty registration name, which is what an unnamed provider is.
        now: The instant to record every upsert against. Injected for deterministic tests.
        principal: Who asks. ``admin`` is required (M5-17's rule — the scope is checked in the
            service as well as at the route; F8/M5C-8 closed the one writer that skipped it);
            ``None`` is an internal call with no request behind it (startup, a scheduled
            refresh) and is allowed.

    Returns:
        The counts this run produced, summed across every registration that answered.

    Raises:
        InsufficientScope: ``principal`` is present and below ``admin``; nothing was written.
        ProviderError: **Every** registration failed to list. One that fails among several is
            recorded in :attr:`DiscoveryOutcome.unreachable` and the pass continues, so one dead
            endpoint cannot empty a working registry; with a single registration this is the 1.0
            behaviour unchanged.
    """
    authorize(principal, "admin")
    registrations = _as_registrations(providers)
    listed: list[tuple[ProviderRegistration, Sequence[ModelDescriptor]]] = []
    unreachable: list[str] = []
    first_error: ProviderError | None = None
    for registration in registrations:
        try:
            listed.append((registration, registration.provider.list_models(refresh=True)))
        except ProviderError as exc:
            unreachable.append(registration.name)
            if first_error is None:
                first_error = exc
    if not listed and first_error is not None:
        raise first_error

    seen_canonical_ids: set[str] = set()
    answered_kinds = {registration.kind for registration, _ in listed}
    total = 0

    added = updated = 0
    with database.write() as session:
        for registration, descriptors in listed:
            total += len(descriptors)
            for descriptor in descriptors:
                existing = (
                    session.query(Model)
                    .filter_by(
                        provider_kind=descriptor.identity.provider_kind.value,
                        provider_model_name=descriptor.identity.provider_model_name,
                        artifact_digest=descriptor.identity.artifact_digest,
                    )
                    .one_or_none()
                )
                is_new = existing is None
                model = _upsert_model(
                    session,
                    descriptor,
                    now=now,
                    provider_name=registration.name,
                    is_remote=registration.is_remote,
                )
                _sync_declared_capabilities(session, model, descriptor, now=now)
                seen_canonical_ids.add(model.canonical_id)
                if is_new:
                    added += 1
                else:
                    updated += 1

        unavailable = 0
        for model in session.query(Model).filter_by(available=True).all():
            if model.canonical_id in seen_canonical_ids:
                continue
            # Only a registration that answered can retire its own models. A row served by a
            # kind nothing answered for is left alone, availability unchanged.
            if model.provider_kind not in answered_kinds:
                continue
            model.available = False
            model.unavailable_reason = "not reported by the provider's most recent discovery"
            unavailable += 1

        # ADR-0022 §4: every evidence row's `match_state` is re-evaluated on every discovery
        # pass, inside this same transaction. That is what makes evidence imported before a
        # model was known bind by itself, with no re-import — and what unbinds a row whose
        # model this pass just upgraded away from the identity it was bound to.
        rebind_evidence_in(session)

    return DiscoveryOutcome(
        added=added,
        updated=updated,
        unavailable=unavailable,
        total=total,
        checked_at=now,
        unreachable=tuple(unreachable),
    )


def set_model_enabled(
    database: Database,
    *,
    model_id: str,
    enabled: bool,
    principal: Principal | None = None,
) -> None:
    """Permit or refuse this model, and nothing else (ADR-0118).

    The flag is an operator's decision. Discovery never writes it, so a disabled model that
    disappears from the provider and comes back is still disabled, and nothing about the model's
    evidence, reliability or history changes either way.

    Args:
        database: The application's database handle.
        model_id: The registry ULID of the row to change.
        enabled: Whether the model may be routed to and executed on.
        principal: Who asks; ``admin`` is required, as it is for discovery.

    Raises:
        InsufficientScope: ``principal`` is below ``admin``; nothing was written.
        ValidationError: No registry row has that id.
    """
    authorize(principal, "admin")
    with database.write() as session:
        model = session.get(Model, model_id)
        if model is None:
            message = f"no model with id {model_id!r} is in the registry."
            raise ValidationError(message, details={"field": "model_id", "value": model_id})
        model.enabled = enabled


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """One model as shown by ``GET /models`` and ``loadcoach models list``."""

    canonical_id: str
    provider_kind: str
    provider_model_name: str
    identity_confidence: str
    family: str | None
    quantization: str | None
    max_context: int | None
    size_bytes: int | None
    parameter_count: int | None
    available: bool
    unavailable_reason: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    declared_capabilities: dict[str, float]
    model_id: str = ""
    """The registry ULID — api.md §2's ``model_ref``, the form that survives a path segment."""
    provider_name: str = ""
    """The registration that served this model's most recent discovery (ADR-0055). ``""`` reads
    as "not recorded" — a row discovered before registrations had names."""
    is_remote: bool = False
    """The registration's declared egress class, never inferred from the kind (ADR-0055 rule 4)."""
    enabled: bool = True
    """Whether an operator permits this model to be used (ADR-0118). Not ``available``: that is
    the provider's report, this is a person's decision, and neither implies the other."""


def list_registry(database: Database) -> tuple[RegistryEntry, ...]:
    """Return every known model, available or not, newest-seen first."""
    with database.read() as session:
        models = session.query(Model).order_by(Model.last_seen_at.desc()).all()
        entries = []
        for model in models:
            capability_rows = (
                session.query(ModelCapability).filter_by(model_id=model.id, source="declared").all()
            )
            entries.append(
                RegistryEntry(
                    canonical_id=model.canonical_id,
                    provider_kind=model.provider_kind,
                    provider_model_name=model.provider_model_name,
                    identity_confidence=model.identity_confidence,
                    family=model.family,
                    quantization=model.quantization,
                    max_context=model.max_context,
                    size_bytes=model.size_bytes,
                    parameter_count=model.parameter_count,
                    available=model.available,
                    unavailable_reason=model.unavailable_reason,
                    first_seen_at=model.first_seen_at,
                    last_seen_at=model.last_seen_at,
                    declared_capabilities={
                        row.capability_id: row.score
                        for row in capability_rows
                        if row.score is not None
                    },
                    model_id=model.id,
                    provider_name=model.provider_name,
                    is_remote=model.is_remote,
                    enabled=model.enabled,
                )
            )
        return tuple(entries)


@dataclass(frozen=True, slots=True)
class ModelOverview:
    """One model as the Models page and ``GET /models`` show it (api.md §2, dev-plan P8).

    Attributes:
        entry: Identity, availability and declared capabilities.
        evidence: Imported evidence for the model: ``bound`` records, distinct ``capabilities``
            measured, ``stale`` and ``unmatched`` counts.
        reliability: Production statistics across task profiles: ``pairs`` tracked, counted
            ``attempts_7d``, the ``lowest_factor`` routing applies on any profile (``None`` when
            every pair is neutral), ``regressions``, and the breaker's ``circuit_state``.
        residency: ``resident`` and the ``gpu_indexes`` it is loaded on.
        adapters: The adapter subjects this base can serve, in name order — each with its subject
            string, its base-identity confidence, its classification, the evidence source routing
            would use for it and the registration that serves the base. Empty for a deployment
            with no adapter directory, which is every 1.0 deployment.
    """

    entry: RegistryEntry
    evidence: dict[str, int]
    reliability: dict[str, Any]
    residency: dict[str, Any]
    adapters: tuple[dict[str, Any], ...] = ()

    def as_json(self) -> dict[str, Any]:
        """The three summaries ``GET /models`` carries beside the identity fields."""
        return {
            "evidence_summary": dict(self.evidence),
            "reliability": dict(self.reliability),
            "residency": dict(self.residency),
            "adapters": [dict(adapter) for adapter in self.adapters],
        }


def registry_overview(database: Database) -> tuple[ModelOverview, ...]:
    """Every model with its evidence, reliability and residency summaries (api.md §2)."""
    from sqlalchemy import func, select

    from loadcoach.infrastructure.db.models import Adapter, CapabilityEvidence, Residency
    from loadcoach.services.reliability import reliability_report

    entries = list_registry(database)
    with database.read() as session:
        evidence_rows = session.execute(
            select(
                CapabilityEvidence.model_id,
                CapabilityEvidence.match_state,
                CapabilityEvidence.stale,
                func.count(),
                func.count(func.distinct(CapabilityEvidence.capability_id)),
            )
            .where(CapabilityEvidence.model_id.is_not(None))
            .group_by(
                CapabilityEvidence.model_id,
                CapabilityEvidence.match_state,
                CapabilityEvidence.stale,
            )
        ).all()
        resident_rows = session.execute(
            select(Residency.model_id, Residency.gpu_index).where(Residency.resident.is_(True))
        ).all()
        adapter_rows = list(session.execute(select(Adapter).order_by(Adapter.name)).scalars().all())
    evidence: dict[str, dict[str, int]] = {}
    for model_id, match_state, stale, count, capabilities in evidence_rows:
        coverage = evidence.setdefault(
            model_id, {"bound": 0, "capabilities": 0, "stale": 0, "unmatched": 0}
        )
        if match_state == "bound":
            coverage["bound"] += int(count)
            coverage["capabilities"] = max(coverage["capabilities"], int(capabilities))
            if stale:
                coverage["stale"] += int(count)
        else:
            coverage["unmatched"] += int(count)
    residency: dict[str, list[int]] = {}
    for model_id, gpu_index in resident_rows:
        residency.setdefault(model_id, []).append(int(gpu_index))
    reliability: dict[str, dict[str, Any]] = {}
    for report in reliability_report(database):
        summary: dict[str, Any] = reliability.setdefault(
            report.model_id,
            {
                "pairs": 0,
                "attempts_7d": 0,
                "lowest_factor": None,
                "regressions": 0,
                "circuit_state": "closed",
            },
        )
        summary["pairs"] += 1
        summary["attempts_7d"] += report.windows["7d"].counted
        if not report.factor.neutral:
            lowest = summary["lowest_factor"]
            summary["lowest_factor"] = (
                report.factor.value if lowest is None else min(lowest, report.factor.value)
            )
        if report.regression.regressed:
            summary["regressions"] += 1
        if report.circuit_state != "closed":
            summary["circuit_state"] = report.circuit_state
    adapters_by_base: dict[str, list[Adapter]] = {}
    for adapter in adapter_rows:
        adapters_by_base.setdefault(adapter.base_model_name, []).append(adapter)
    return tuple(
        ModelOverview(
            entry=entry,
            adapters=_adapter_views(entry, adapters_by_base.get(entry.provider_model_name, [])),
            evidence=evidence.get(
                entry.model_id, {"bound": 0, "capabilities": 0, "stale": 0, "unmatched": 0}
            ),
            reliability=reliability.get(
                entry.model_id,
                {
                    "pairs": 0,
                    "attempts_7d": 0,
                    "lowest_factor": None,
                    "regressions": 0,
                    "circuit_state": "closed",
                },
            ),
            residency={
                "resident": entry.model_id in residency,
                "gpu_indexes": sorted(residency.get(entry.model_id, [])),
            },
        )
        for entry in entries
    )


def _adapter_views(entry: RegistryEntry, adapters: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    """Render the adapter subjects one base can serve, for the models view (dev-plan P10).

    ``evidence_source`` is what routing would actually use for the subject: ``"declared"`` where
    the manifest claims vocabulary terms, and ``"absent"`` where it claims none. It is never
    ``"benchmark"`` today, because an adapter subject inherits nothing from its base and nothing
    measures adapters until FreeWeight does (LA3) — which is exactly why the shipped
    ``require_adapter_evidence`` makes adapters invisible to routed selection.
    """
    from loadcoach.services.reliability import subject_id_of

    views: list[dict[str, Any]] = []
    for adapter in adapters:
        declared = list(adapter.declared_capabilities_json or [])
        views.append(
            {
                "name": adapter.name,
                "subject_canonical_id": subject_id_of(entry.canonical_id, adapter),
                "artifact_digest": adapter.artifact_sha256,
                "base_confidence": adapter.base_identity_confidence,
                "data_classification": adapter.data_classification,
                "declared_capabilities": declared,
                "evidence_source": "declared" if declared else "absent",
                "provider_name": entry.provider_name or None,
                "available": adapter.available,
                "unavailable_reason": adapter.unavailable_reason,
            }
        )
    return tuple(views)


def try_discover_models(
    database: Database,
    providers: Provider | Sequence[ProviderRegistration],
    *,
    now: datetime,
) -> DiscoveryOutcome | None:
    """Run :func:`discover_models`, returning ``None`` instead of raising on a provider failure.

    For call sites (startup, a background refresh) that must not fail the whole operation just
    because the provider is unreachable — spec §5: LoadCoach starts and serves with no provider.
    """
    try:
        return discover_models(database, providers, now=now)
    except ProviderError:
        return None


def import_manual_capability_scores(
    database: Database, *, path: Path = DEFAULT_MANUAL_SCORES_PATH, now: datetime
) -> int:
    """Import operator-entered capability scores (dev-plan P2: "Manual capability scores from
    configuration, marked source: manual").

    A score naming a model LoadCoach has not discovered yet is skipped, not an error — discovery
    order is not guaranteed, and an operator's file predating a model's first sighting is a normal
    state, not a mistake.

    Args:
        database: The application's database handle.
        path: The TOML file to read. Missing entirely is not an error — it means no manual scores
            are configured, the shipped default.
        now: The instant to record; injected for deterministic tests.

    Returns:
        How many scores were imported (excludes entries skipped for an undiscovered model).

    Raises:
        ManualScoreInvalid: An entry is malformed — see
            :func:`loadcoach.domain.registry.validate_manual_score`.
    """
    if not path.is_file():
        return 0
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    entries = raw.get("scores", [])

    validated = [
        validate_manual_score(str(path), index, entry) for index, entry in enumerate(entries)
    ]

    imported = 0
    with database.write() as session:
        for entry in validated:
            model = session.query(Model).filter_by(canonical_id=entry.canonical_id).one_or_none()
            if model is None:
                continue
            upsert(
                session,
                ModelCapability,
                {
                    "model_id": model.id,
                    "capability_id": entry.capability_id,
                    "score": entry.score,
                    "confidence": entry.confidence,
                    "source": "manual",
                    "updated_at": now,
                },
                index_elements=["model_id", "capability_id", "source"],
            )
            imported += 1
    return imported
