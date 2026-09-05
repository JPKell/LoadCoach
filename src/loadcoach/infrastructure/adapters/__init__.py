"""Reading the operator's adapter directory (ADR-0061).

The registry is a directory of artifacts plus one reviewed manifest per adapter — not a service,
not a shared database, and not a naming convention. This package reads it and converts what it
finds into ModelRack's :class:`~modelrack.adapters.AdapterRegistration`; that conversion lives in
the application because ModelRack never reads a directory
(``docs/history/F3_HANDOFF.md`` §2).
"""

from __future__ import annotations

from loadcoach.infrastructure.adapters.directory import (
    DRAFT_SUFFIX,
    MANIFEST_SUFFIX,
    AdapterEntry,
    DirectoryReading,
    DraftOutcome,
    draft_manifests,
    read_directory,
    registrations_from,
)

__all__ = [
    "DRAFT_SUFFIX",
    "MANIFEST_SUFFIX",
    "AdapterEntry",
    "DirectoryReading",
    "DraftOutcome",
    "draft_manifests",
    "read_directory",
    "registrations_from",
]
