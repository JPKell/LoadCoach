"""The adapter registry, as the CLI and the UI see it (ADR-0061).

Three questions, one service: what does the directory hold, what does each registration's provider
make of it, and what would a scan write. Nothing here decides routing — that is the domain's job,
and it arrives with subject expansion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

from baseaicore import SuiteError
from sqlalchemy import select
from weightsdb import upsert

from loadcoach.domain.authorization import Principal, authorize
from loadcoach.domain.routing.subject import AdapterFacts
from loadcoach.infrastructure.adapters import (
    AdapterEntry,
    DraftOutcome,
    draft_manifests,
    read_directory,
)
from loadcoach.infrastructure.db.models import Adapter
from loadcoach.services.evidence import rebind_evidence_in

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from loadcoach.config import Settings
    from loadcoach.infrastructure.providers.factory import ProviderRegistration
    from loadcoach.services.database import Database

__all__ = [
    "AdapterNotFound",
    "AdapterOverview",
    "AdaptersDisabled",
    "AdapterView",
    "adapter_facts_by_base_name",
    "adapter_overview",
    "adapter_report",
    "scan_adapters",
    "show_adapter",
    "sync_adapters",
]


class AdaptersDisabled(SuiteError):
    """``[adapters] directory`` is empty, so the feature is off (ADR-0061 rule 2)."""

    code: ClassVar[str] = "CONFIGURATION_ERROR"

    def __init__(self) -> None:
        """Say which key turns it on, because "nothing happened" is not a diagnosis."""
        super().__init__(
            "adapters are not configured: set [adapters] directory to the directory holding your "
            "adapter artifacts and their reviewed manifests. Empty means off, deliberately."
        )


class AdapterNotFound(SuiteError):
    """No adapter of that name is available — in the directory, or on the provider that would serve.

    A pin is an assertion, so it fails loudly rather than falling back to the bare base
    (ADR-0064 rule 4). ``details`` names the adapter that was asked for and the names that exist,
    because "not found" without the alternatives is a dead end (spec §13, api.md §10).
    """

    code: ClassVar[str] = "ADAPTER_NOT_FOUND"


@dataclass(frozen=True, slots=True)
class AdapterView:
    """One adapter as a person reads it.

    Attributes:
        entry: What the directory says.
        registered_on: Registration names whose providers hold this adapter and can select it
            now. Empty is not an error: a provider that cannot hot-swap was never offered it.
        pending_on: Registration names whose providers hold it but need a restart before it can
            be selected — ``AdapterStatus.PENDING_RESTART``.
        provider_notes: ``(registration name, prose)`` for every state a provider reported.
            Prose, for a person; nothing parses it.
    """

    entry: AdapterEntry
    registered_on: tuple[str, ...] = ()
    pending_on: tuple[str, ...] = ()
    provider_notes: tuple[tuple[str, str], ...] = ()

    def as_json(self) -> dict[str, Any]:
        """Render for ``--json`` and for the models view."""
        entry = self.entry
        return {
            "name": entry.name,
            "artifact_sha256": entry.artifact_sha256,
            "artifact_path": str(entry.artifact_path),
            "manifest_path": str(entry.manifest_path),
            "base_model_name": entry.base_model_name,
            "base_artifact_digest": entry.base_artifact_digest,
            "base_confidence": entry.base_confidence.value,
            "declared_capabilities": list(entry.declared_capabilities),
            "data_classification": entry.data_classification.value,
            "available": entry.available,
            "unavailable_reason": entry.unavailable_reason,
            "registered_on": list(self.registered_on),
            "pending_on": list(self.pending_on),
            "notes": entry.notes,
        }


@dataclass(frozen=True, slots=True)
class AdapterOverview:
    """Every adapter, plus what the directory could not read.

    Attributes:
        directory: The directory read.
        adapters: One view per reviewed manifest, in name order.
        invalid: ``(path, problem)`` per manifest that could not be read at all.
        drafts: Draft manifests waiting for a person. Nothing trusts them.
        unmanifested: Artifacts a scan would draft for.
    """

    directory: Path
    adapters: tuple[AdapterView, ...] = ()
    invalid: tuple[tuple[Path, str], ...] = ()
    drafts: tuple[Path, ...] = ()
    unmanifested: tuple[Path, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """Render for ``--json``."""
        return {
            "directory": str(self.directory),
            "adapters": [view.as_json() for view in self.adapters],
            "invalid": [{"path": str(path), "problem": problem} for path, problem in self.invalid],
            "drafts": [str(path) for path in self.drafts],
            "unmanifested": [str(path) for path in self.unmanifested],
        }


def adapter_overview(
    settings: Settings,
    registrations: tuple[ProviderRegistration, ...] = (),
    *,
    principal: Principal | None = None,
) -> AdapterOverview:
    """Read the directory and ask every hot-swapping provider what it makes of it.

    Args:
        settings: The resolved configuration.
        registrations: The registrations to ask. Empty reports the directory alone, which is what
            a one-shot command with no running providers can honestly say.
        principal: Who asks; ``read`` is enough. ``None`` is an internal call.

    Returns:
        The :class:`AdapterOverview`.

    Raises:
        AdaptersDisabled: ``[adapters] directory`` is empty.
        InsufficientScope: ``principal`` is below ``read``.
    """
    authorize(principal, "read")
    directory = settings.adapters.path
    if directory is None:
        raise AdaptersDisabled
    reading = read_directory(directory)
    states = _states_by_adapter(registrations)
    return AdapterOverview(
        directory=directory,
        adapters=tuple(_view(entry, states) for entry in reading.entries),
        invalid=reading.invalid,
        drafts=reading.drafts,
        unmanifested=reading.unmanifested,
    )


def show_adapter(
    name: str,
    settings: Settings,
    registrations: tuple[ProviderRegistration, ...] = (),
    *,
    principal: Principal | None = None,
) -> AdapterView:
    """Return one adapter by name.

    Raises:
        AdaptersDisabled: The feature is off.
        AdapterNotFound: No reviewed manifest in the directory carries that name. A *draft* is
            not a match: nothing trusts an unreviewed draft, so saying "not found" is the honest
            answer even when a file with that name is sitting there.
        InsufficientScope: ``principal`` is below ``read``.
    """
    overview = adapter_overview(settings, registrations, principal=principal)
    for view in overview.adapters:
        if view.entry.name == name:
            return view
    names = [view.entry.name for view in overview.adapters]
    known = ", ".join(names) or "none"
    message = f"no adapter named {name!r} in {overview.directory}; known adapters: {known}"
    raise AdapterNotFound(message, details={"adapter": name, "known_adapters": names})


def scan_adapters(
    settings: Settings, *, now: datetime | None = None, principal: Principal | None = None
) -> DraftOutcome:
    """Draft a manifest for every artifact that has none (ADR-0061 rule 4).

    The scan drafts; a human keeps. Nothing this writes is registered until a person reviews it
    and renames it, and no manifest that already exists is touched.

    Raises:
        AdaptersDisabled: The feature is off.
        FileNotFoundError: The configured directory does not exist.
        InsufficientScope: ``principal`` is below ``admin`` — a scan writes files.
    """
    authorize(principal, "admin")
    directory = settings.adapters.path
    if directory is None:
        raise AdaptersDisabled
    return draft_manifests(directory, now=now)


def _states_by_adapter(
    registrations: tuple[ProviderRegistration, ...],
) -> dict[str, list[tuple[str, str, str]]]:
    """Collect ``(registration, status, reason)`` per adapter name, from the providers that hold it.

    A provider that declares no ``adapter_hot_swap`` refuses ``list_adapters`` by contract, and
    that refusal is not a fault: it means this provider has no concept of adapters, which is a
    different fact from holding none.
    """
    from modelrack.errors import CapabilityUnsupported, ProviderError

    collected: dict[str, list[tuple[str, str, str]]] = {}
    for registration in registrations:
        try:
            states = registration.provider.list_adapters()
        except (CapabilityUnsupported, ProviderError):
            continue
        for state in states:
            collected.setdefault(state.adapter.name, []).append(
                (registration.name, state.status.value, state.reason or "")
            )
    return collected


def _view(entry: AdapterEntry, states: dict[str, list[tuple[str, str, str]]]) -> AdapterView:
    reported = states.get(entry.name, [])
    return AdapterView(
        entry=entry,
        registered_on=tuple(name for name, status, _ in reported if status == "registered"),
        pending_on=tuple(name for name, status, _ in reported if status == "pending_restart"),
        provider_notes=tuple((name, reason) for name, _, reason in reported),
    )


def sync_adapters(database: Database, settings: Settings, *, now: datetime) -> int:
    """Read the configured directory into the ``adapters`` table, and return how many rows it holds.

    The directory is the truth and the table is its projection: identity is the artifact hash, so a
    renamed artifact updates ``artifact_path`` on the row it already had, and an edited one is a
    new row (ADR-0061 rule 5). Routing reads the table rather than the directory because reading
    the directory means hashing every artifact, which is file I/O no routing decision may do.

    A row whose adapter has left the directory is **kept and marked unavailable**, not deleted:
    a stored decision names it by foreign key, and deleting the row would orphan an explanation
    that must stay readable (ADR-0080).

    **Evidence is re-bound in the same transaction**, because this pass is discovery for the
    subject's second axis. Imported evidence measured on ``(base, adapterA)`` waits ``unmatched``
    until this registry can hold that adapter, and ADR-0022 §4 requires it to bind on the next
    discovery pass **with no re-import** — so the scan that first sees the adapter is the pass that
    has to do it. Left to the model discovery pass, the record would sit unmatched until something
    unrelated happened to the model registry.

    Args:
        database: The application's database handle.
        settings: The resolved configuration. With ``[adapters] directory`` empty this is a no-op
            returning ``0`` — the feature is off, and nothing is written.
        now: The instant of this pass. Injected, so a test can assert ``last_seen_at``.

    Returns:
        The number of adapters the directory described, available or not.
    """
    directory = settings.adapters.path
    if directory is None:
        return 0
    reading = read_directory(directory)
    seen = {entry.artifact_sha256 for entry in reading.entries}
    with database.write() as session:
        for entry in reading.entries:
            upsert(
                session,
                Adapter,
                {
                    "name": entry.name,
                    "artifact_sha256": entry.artifact_sha256,
                    "source_sha256": entry.source_sha256,
                    "artifact_path": str(entry.artifact_path),
                    "manifest_path": str(entry.manifest_path),
                    "base_model_name": entry.base_model_name,
                    "base_artifact_digest": entry.base_artifact_digest,
                    "base_identity_confidence": entry.base_confidence.value,
                    "declared_capabilities_json": list(entry.declared_capabilities),
                    "data_classification": entry.data_classification.value,
                    "adapter_format": "gguf",
                    "manifest_json": entry.payload,
                    "created_at": now,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "available": entry.available,
                    "unavailable_reason": entry.unavailable_reason,
                },
                index_elements=["artifact_sha256"],
                no_update=frozenset({"created_at", "first_seen_at"}),
            )
        for row in session.execute(select(Adapter)).scalars().all():
            if row.artifact_sha256 not in seen and row.available:
                row.available = False
                row.unavailable_reason = (
                    "no reviewed manifest in the configured directory names this artifact any "
                    "more; the row is kept because stored decisions name it"
                )
        session.flush()
        rebind_evidence_in(session)
    return len(reading.entries)


def adapter_facts_by_base_name(database: Database) -> dict[str, tuple[AdapterFacts, ...]]:
    """Return every available adapter, grouped by the base model name it declares.

    The grouping key is the **name**, not the digest, on purpose: an adapter whose manifest
    declares a base digest that does not match the served base must become a candidate and be
    *rejected by name* (``adapter_incompatible``), not silently vanish. Grouping on the digest
    would make that mismatch invisible, which is the confusion ADR-0061 exists to prevent.

    Args:
        database: The application's database handle.

    Returns:
        Base model name -> its adapters, each in name order. Empty when adapters are off or the
        directory has never been synced.
    """
    with database.read() as session:
        rows = (
            session.execute(
                select(Adapter).where(Adapter.available.is_(True)).order_by(Adapter.name)
            )
            .scalars()
            .all()
        )
    grouped: dict[str, list[AdapterFacts]] = {}
    for row in rows:
        grouped.setdefault(row.base_model_name, []).append(
            AdapterFacts(
                adapter_id=row.id,
                name=row.name,
                artifact_digest=row.artifact_sha256,
                base_model_name=row.base_model_name,
                base_artifact_digest=row.base_artifact_digest,
                base_confidence=row.base_identity_confidence,
                declared_capabilities=tuple(
                    cast("list[str]", row.declared_capabilities_json or [])
                ),
                data_classification=row.data_classification,
            )
        )
    return {name: tuple(facts) for name, facts in grouped.items()}


def adapter_report(
    database: Database,
    settings: Settings,
    registrations: tuple[ProviderRegistration, ...] = (),
    *,
    principal: Principal | None = None,
    route_limit: int = 20,
) -> dict[str, Any]:
    """``GET /adapters``' document: every adapter, where it is held, resident, routed (api.md §2).

    The directory is the truth (ADR-0061), so the list starts from :func:`adapter_overview`; the
    ``adapters`` table is joined in only for what the directory cannot say — the row id that
    residency and routing candidates name. A row whose artifact has left the directory is still
    listed, ``in_directory: false``, because a stored decision names it and deleting it from the
    view would orphan that decision as surely as deleting the row would.

    Args:
        database: The application's database handle.
        settings: The resolved configuration.
        registrations: The live registrations, asked what each holds.
        principal: Who asks; ``read`` is enough. ``None`` is an internal call.
        route_limit: The routing candidates listed per adapter, newest first.

    Returns:
        ``enabled``, ``note``, ``directory``, ``adapters``, ``invalid``, ``drafts``,
        ``unmanifested``. With ``[adapters] directory`` unset, ``enabled`` is ``false`` and ``note``
        names the key — the feature being off is a state, not a failure.

    Raises:
        InsufficientScope: ``principal`` is below ``read``.
    """
    from baseaicore.timeutil import to_rfc3339

    from loadcoach.infrastructure.db.models import (
        Model,
        Residency,
        RoutingCandidate,
        RoutingDecision,
    )

    authorize(principal, "read")
    try:
        overview = adapter_overview(settings, registrations, principal=principal)
    except AdaptersDisabled as disabled:
        return {
            "enabled": False,
            "note": disabled.message,
            "directory": None,
            "adapters": [],
            "invalid": [],
            "drafts": [],
            "unmanifested": [],
        }
    document = overview.as_json()
    with database.read() as session:
        rows = session.execute(select(Adapter).order_by(Adapter.name)).scalars().all()
        by_digest = {row.artifact_sha256: row for row in rows}
        entries: list[dict[str, Any]] = []
        for view in document["adapters"]:
            row = by_digest.pop(view["artifact_sha256"], None)
            entries.append(
                {**view, "in_directory": True, "adapter_id": None if row is None else row.id}
            )
        entries.extend(
            {
                "name": row.name,
                "artifact_sha256": row.artifact_sha256,
                "artifact_path": row.artifact_path,
                "manifest_path": row.manifest_path,
                "base_model_name": row.base_model_name,
                "base_artifact_digest": row.base_artifact_digest,
                "base_confidence": row.base_identity_confidence,
                "declared_capabilities": list(
                    cast("list[str]", row.declared_capabilities_json or [])
                ),
                "data_classification": row.data_classification,
                "available": row.available,
                "unavailable_reason": row.unavailable_reason,
                "registered_on": [],
                "pending_on": [],
                "notes": None,
                "in_directory": False,
                "adapter_id": row.id,
            }
            for row in by_digest.values()
        )
        for entry in entries:
            adapter_id = entry["adapter_id"]
            if adapter_id is None:
                entry["resident"], entry["routes"] = [], []
                continue
            resident = session.execute(
                select(Residency, Model.canonical_id)
                .join(Model, Model.id == Residency.model_id)
                .where(Residency.adapter_id == adapter_id, Residency.resident.is_(True))
                .order_by(Residency.gpu_index)
            ).all()
            entry["resident"] = [
                {
                    "gpu_index": residency.gpu_index,
                    "base_canonical_id": canonical_id,
                    "last_used_at": to_rfc3339(residency.last_used_at),
                }
                for residency, canonical_id in resident
            ]
            routed = session.execute(
                select(RoutingCandidate, RoutingDecision)
                .join(RoutingDecision, RoutingDecision.id == RoutingCandidate.decision_id)
                .where(RoutingCandidate.adapter_id == adapter_id)
                .order_by(RoutingDecision.requested_at.desc())
                .limit(route_limit)
            ).all()
            entry["routes"] = [
                {
                    "decision_id": decision.id,
                    "job_id": decision.job_id,
                    "task_profile_id": decision.task_profile_id,
                    "requested_at": to_rfc3339(decision.requested_at),
                    "rank": candidate.rank,
                    "rejected": bool(candidate.rejected),
                    "rejection_reason": candidate.rejection_reason,
                    "selected": decision.selected_adapter_id == adapter_id
                    and decision.selected_model_id == candidate.model_id,
                }
                for candidate, decision in routed
            ]
    return {"enabled": True, "note": None, **document, "adapters": entries}
