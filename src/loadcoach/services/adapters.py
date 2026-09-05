"""The adapter registry, as the CLI and the UI see it (ADR-0061).

Three questions, one service: what does the directory hold, what does each registration's provider
make of it, and what would a scan write. Nothing here decides routing — that is the domain's job,
and it arrives with subject expansion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loadcoach.domain.authorization import Principal, authorize
from loadcoach.infrastructure.adapters import (
    AdapterEntry,
    DraftOutcome,
    draft_manifests,
    read_directory,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from loadcoach.config import Settings
    from loadcoach.infrastructure.providers.factory import ProviderRegistration

__all__ = [
    "AdapterNotFound",
    "AdapterOverview",
    "AdaptersDisabled",
    "AdapterView",
    "adapter_overview",
    "scan_adapters",
    "show_adapter",
]


class AdaptersDisabled(Exception):
    """``[adapters] directory`` is empty, so the feature is off (ADR-0061 rule 2)."""

    def __init__(self) -> None:
        """Say which key turns it on, because "nothing happened" is not a diagnosis."""
        super().__init__(
            "adapters are not configured: set [adapters] directory to the directory holding your "
            "adapter artifacts and their reviewed manifests. Empty means off, deliberately."
        )


class AdapterNotFound(Exception):
    """No adapter of that name is in the directory."""


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
    known = ", ".join(view.entry.name for view in overview.adapters) or "none"
    message = f"no adapter named {name!r} in {overview.directory}; known adapters: {known}"
    raise AdapterNotFound(message)


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
