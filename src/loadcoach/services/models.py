"""loadcoach.services.models — discovery through ModelRack and the registry read path.

Unavailable models are flagged with a reason, never deleted (dev-plan P2 test list) — history of
what a machine has seen outlives whether it can see it right now, the same principle database
standards §8 states for results.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from baseaicore import is_supported
from modelrack import ProviderError
from weightsdb import upsert

from loadcoach.domain.registry import (
    declared_capabilities_for,
    descriptor_geometry,
    validate_manual_score,
)
from loadcoach.infrastructure.db.models import Model, ModelCapability
from loadcoach.services.evidence import rebind_evidence_in

if TYPE_CHECKING:
    from collections.abc import Sequence

    from baseaicore import ModelDescriptor
    from modelrack.provider import Provider
    from sqlalchemy.orm import Session

    from loadcoach.services.database import Database

__all__ = [
    "DEFAULT_MANUAL_SCORES_PATH",
    "DiscoveryOutcome",
    "RegistryEntry",
    "discover_models",
    "import_manual_capability_scores",
    "list_registry",
    "try_discover_models",
]

DEFAULT_MANUAL_SCORES_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "manual_capability_scores.toml"
)


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    """The result of one :func:`discover_models` pass."""

    added: int
    updated: int
    unavailable: int
    total: int
    checked_at: datetime


def _upsert_model(session: Session, descriptor: ModelDescriptor, *, now: datetime) -> Model:
    """Insert or update ``descriptor``'s identity row, upgrading a name-only sibling in place.

    Mirrors FreeWeight's own identity resolution (its ``ModelRepository.upsert_identity``): the
    natural key ``(provider_kind, provider_model_name, artifact_digest)`` has a partial unique
    index for the ``artifact_digest IS NULL`` case (data model §2, ``uq_models_name_only``), which
    ``weightsdb.upsert()`` cannot target (it does not support partial indexes — its own docstring
    names this exact case). A digest-confirmed sighting of a model LoadCoach previously only knew
    by name upgrades that row's identity rather than creating a second, duplicate one.
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


def discover_models(database: Database, provider: Provider, *, now: datetime) -> DiscoveryOutcome:
    """Run one discovery pass: list every model the provider serves and persist it.

    A model previously discovered but absent from this pass is marked ``available=False`` with a
    reason — never deleted (dev-plan P2 test list, database standards §8).

    Imported evidence is re-bound in the same transaction (ADR-0022 §4), so a bundle that arrived
    before its models were discovered starts scoring on this pass rather than on a re-import.

    Args:
        database: The application's database handle.
        provider: The provider to discover through.
        now: The instant to record every upsert against. Injected for deterministic tests.

    Returns:
        The counts this run produced.

    Raises:
        ProviderError: The provider could not be listed at all (unreachable, timed out, or
            answered with something ModelRack could not parse).
    """
    descriptors: Sequence[ModelDescriptor] = provider.list_models(refresh=True)
    seen_canonical_ids: set[str] = set()

    added = updated = 0
    with database.write() as session:
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
            model = _upsert_model(session, descriptor, now=now)
            _sync_declared_capabilities(session, model, descriptor, now=now)
            seen_canonical_ids.add(model.canonical_id)
            if is_new:
                added += 1
            else:
                updated += 1

        unavailable = 0
        for model in session.query(Model).filter_by(available=True).all():
            if model.canonical_id not in seen_canonical_ids:
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
        total=len(descriptors),
        checked_at=now,
    )


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
                )
            )
        return tuple(entries)


def try_discover_models(
    database: Database, provider: Provider, *, now: datetime
) -> DiscoveryOutcome | None:
    """Run :func:`discover_models`, returning ``None`` instead of raising on a provider failure.

    For call sites (startup, a background refresh) that must not fail the whole operation just
    because the provider is unreachable — spec §5: LoadCoach starts and serves with no provider.
    """
    try:
        return discover_models(database, provider, now=now)
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
