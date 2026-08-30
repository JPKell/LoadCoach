"""loadcoach.services.residency — what is loaded where, and the policy that decides it (queue §6).

ModelRack answers *what is loaded* and *load this* / *unload that*; deciding which model should
be resident is LoadCoach's job, and this is where it lives. The ``residency`` table records every
residency episode per device (ADR-0027: ``max_resident_models`` is interpreted per ``gpu_index``),
and three rules use it:

* **Load before executing**, evicting the least-recently-used idle resident on the target device
  while the device holds ``max_resident_models`` already or lacks the room the estimate needs.
  A model in use by an in-flight job is never evicted from under it.
* **Unload after ``unload_idle_seconds``** of disuse, per device, on the scheduler thread.
* **Prefer the resident model** among close candidates — routing's ``residency_factor`` — and
  batch jobs by affinity, both fed by :meth:`ResidencyService.resident_model_ids`.

A provider that declares neither ``residency_query`` nor ``force_unload`` skips all of this: the
behaviour degrades to load-on-demand with the reason ``residency_unmanaged`` recorded on the
attempt's event, exactly as queue §6 says it should.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from baseaicore import ModelIdentity, ProviderKind, is_supported
from modelrack import ProviderError, residency_support
from sqlalchemy import select

from loadcoach.infrastructure.db.models import Model, Residency

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from baseaicore import RuntimeProfile
    from modelrack import ResidencySupport
    from modelrack.provider import Provider

    from loadcoach.config import ResidencySettings
    from loadcoach.services.database import Database

__all__ = ["LoadOutcome", "ResidencyService", "ResidentEntry"]

logger = logging.getLogger(__name__)

REASON_UNMANAGED = "residency_unmanaged"
"""Recorded when the provider cannot report or control residency (queue §6)."""


@dataclass(frozen=True, slots=True)
class ResidentEntry:
    """One resident model on one device, as the table knows it."""

    residency_id: str
    model_id: str
    canonical_id: str
    provider_kind: str
    provider_model_name: str
    artifact_digest: str | None
    gpu_index: int
    loaded_at: datetime
    last_used_at: datetime
    vram_bytes: int | None

    @property
    def identity(self) -> ModelIdentity:
        """The identity ModelRack's ``load``/``unload`` take."""
        return ModelIdentity(
            provider_kind=ProviderKind(self.provider_kind),
            provider_model_name=self.provider_model_name,
            artifact_digest=self.artifact_digest,
        )


@dataclass(frozen=True, slots=True)
class LoadOutcome:
    """What :meth:`ResidencyService.ensure_loaded` did, for the attempt's event."""

    loaded: bool
    already_resident: bool
    evicted: tuple[str, ...]
    reason: str | None = None

    def as_json(self) -> dict[str, Any]:
        """The event body."""
        return {
            "loaded": self.loaded,
            "already_resident": self.already_resident,
            "evicted": list(self.evicted),
            "reason": self.reason,
        }


class ResidencyService:
    """The residency policy over the ``residency`` table and one provider."""

    def __init__(
        self,
        database: Database,
        provider: Provider,
        *,
        settings: ResidencySettings,
        clock: Callable[[], datetime],
    ) -> None:
        """Bind to the database, the provider and ``[residency]``."""
        self._database = database
        self._provider = provider
        self._settings = settings
        self._clock = clock
        self._support: ResidencySupport | None = None

    # ------------------------------------------------------------------------------- support

    @property
    def support(self) -> ResidencySupport:
        """What the provider lets this service do, read once from its declaration."""
        if self._support is None:
            try:
                self._support = residency_support(self._provider.capabilities())
            except ProviderError:
                from modelrack import ResidencySupport

                return ResidencySupport()
        return self._support

    @property
    def manageable(self) -> bool:
        """Whether a residency policy can run at all against this provider."""
        return self.support.is_manageable

    # ------------------------------------------------------------------------------- reading

    def resident(self) -> tuple[ResidentEntry, ...]:
        """Every resident episode still open, oldest use first."""
        with self._database.read() as session:
            rows = session.execute(
                select(Residency, Model)
                .join(Model, Model.id == Residency.model_id)
                .where(Residency.resident.is_(True))
                .order_by(Residency.last_used_at.asc(), Residency.id.asc())
            ).all()
            return tuple(
                ResidentEntry(
                    residency_id=row.id,
                    model_id=model.id,
                    canonical_id=model.canonical_id,
                    provider_kind=model.provider_kind,
                    provider_model_name=model.provider_model_name,
                    artifact_digest=model.artifact_digest,
                    gpu_index=row.gpu_index,
                    loaded_at=row.loaded_at,
                    last_used_at=row.last_used_at,
                    vram_bytes=None if row.vram_bytes is None else int(row.vram_bytes),
                )
                for row, model in rows
            )

    def resident_model_ids(self) -> frozenset[str]:
        """Registry ULIDs of resident models — the affinity claim's input."""
        return frozenset(entry.model_id for entry in self.resident())

    def resident_canonical_ids(self) -> frozenset[str]:
        """Canonical IDs of resident models — routing's residency tie-break input."""
        return frozenset(entry.canonical_id for entry in self.resident())

    def resident_devices(self) -> dict[str, frozenset[int]]:
        """Canonical ID -> the devices it is resident on — admission's residency exception."""
        devices: dict[str, set[int]] = {}
        for entry in self.resident():
            devices.setdefault(entry.canonical_id, set()).add(entry.gpu_index)
        return {canonical_id: frozenset(found) for canonical_id, found in devices.items()}

    def evictable_bytes_by_device(self, in_use_model_ids: frozenset[str]) -> dict[int, int]:
        """Memory idle residents hold per device — what admission may count as reclaimable."""
        if not self.manageable:
            return {}
        totals: dict[int, int] = {}
        for entry in self.resident():
            if entry.model_id in in_use_model_ids or entry.vram_bytes is None:
                continue
            totals[entry.gpu_index] = totals.get(entry.gpu_index, 0) + entry.vram_bytes
        return totals

    # ------------------------------------------------------------------------------- loading

    def ensure_loaded(
        self,
        *,
        model_id: str,
        canonical_id: str,
        identity: ModelIdentity,
        profile: RuntimeProfile,
        gpu_index: int | None,
        in_use_model_ids: frozenset[str],
        required_bytes: int | None,
        free_bytes: int | None,
        headroom_bytes: int,
        now: datetime,
    ) -> LoadOutcome:
        """Make ``identity`` resident on ``gpu_index`` before a job executes on it.

        Evicts least-recently-used idle residents on the device while it holds
        ``max_resident_models`` or lacks ``required_bytes + headroom_bytes`` of room; never
        evicts a model an in-flight job is using. Records the load and every eviction.

        Args:
            model_id: The registry ULID.
            canonical_id: For the record.
            identity: What the provider loads.
            profile: The runtime profile to load under.
            gpu_index: The device admission chose, or ``None`` on a machine with no GPU — where
                there is no device to be resident on, and the provider loads on demand.
            in_use_model_ids: Models in-flight jobs are executing on; never evicted.
            required_bytes: The candidate's estimate, or ``None`` when unknown.
            free_bytes: The device's free memory as telemetry reports it, or ``None``.
            headroom_bytes: The per-device reserve.
            now: The instant.

        Returns:
            The :class:`LoadOutcome`.
        """
        if not self.manageable:
            return LoadOutcome(
                loaded=False, already_resident=False, evicted=(), reason=REASON_UNMANAGED
            )
        if gpu_index is None:
            return LoadOutcome(loaded=False, already_resident=False, evicted=(), reason="no_device")
        on_device = [entry for entry in self.resident() if entry.gpu_index == gpu_index]
        for entry in on_device:
            if entry.model_id == model_id:
                self.record_use(model_id, gpu_index, now)
                return LoadOutcome(loaded=False, already_resident=True, evicted=())

        evicted: list[str] = []
        free = free_bytes
        for entry in on_device:  # oldest use first
            over_limit = len(on_device) - len(evicted) >= self._settings.max_resident_models
            no_room = (
                required_bytes is not None
                and free is not None
                and free < required_bytes + headroom_bytes
            )
            if not (over_limit or no_room):
                break
            if entry.model_id in in_use_model_ids:
                continue
            if self._unload(entry, reason=f"evicted_for:{canonical_id}", now=now):
                evicted.append(entry.canonical_id)
                if free is not None and entry.vram_bytes is not None:
                    free += entry.vram_bytes

        try:
            self._provider.load(identity, profile)
        except ProviderError as exc:
            logger.warning("residency.load_failed", extra={"canonical_id": canonical_id})
            return LoadOutcome(
                loaded=False, already_resident=False, evicted=tuple(evicted), reason=exc.code
            )
        vram = self._reported_vram(identity)
        with self._database.write() as session:
            session.add(
                Residency(
                    model_id=model_id,
                    gpu_index=gpu_index,
                    loaded_at=now,
                    last_used_at=now,
                    vram_bytes=None if vram is None else float(vram),
                    vram_bytes_unavailable_reason=None
                    if vram is not None
                    else "provider_does_not_report_vram",
                    resident=True,
                )
            )
        logger.info(
            "residency.loaded",
            extra={"canonical_id": canonical_id, "gpu_index": gpu_index, "evicted": evicted},
        )
        return LoadOutcome(loaded=True, already_resident=False, evicted=tuple(evicted))

    def _reported_vram(self, identity: ModelIdentity) -> int | None:
        if not self.support.can_query:
            return None
        try:
            for resident in self._provider.list_resident():
                if resident.identity.provider_model_name == identity.provider_model_name:
                    value = resident.vram_bytes
                    return int(value) if is_supported(value) else None
        except ProviderError:
            return None
        return None

    def record_use(self, model_id: str, gpu_index: int, now: datetime) -> None:
        """Touch ``last_used_at`` for the model on the device — the idle clock's origin."""
        with self._database.write() as session:
            row = session.execute(
                select(Residency).where(
                    Residency.model_id == model_id,
                    Residency.gpu_index == gpu_index,
                    Residency.resident.is_(True),
                )
            ).scalar_one_or_none()
            if row is not None:
                row.last_used_at = now

    # ------------------------------------------------------------------------------ evicting

    def _unload(self, entry: ResidentEntry, *, reason: str, now: datetime) -> bool:
        try:
            self._provider.unload(entry.identity)
        except ProviderError:
            logger.warning("residency.unload_failed", extra={"canonical_id": entry.canonical_id})
            return False
        with self._database.write() as session:
            row = session.get(Residency, entry.residency_id)
            if row is not None:
                row.resident = False
                row.unloaded_at = now
                row.unload_reason = reason
        logger.info(
            "residency.unloaded",
            extra={
                "canonical_id": entry.canonical_id,
                "gpu_index": entry.gpu_index,
                "reason": reason,
            },
        )
        return True

    def evict_idle(self, now: datetime, *, in_use_model_ids: frozenset[str]) -> tuple[str, ...]:
        """Unload every resident model idle for ``unload_idle_seconds``, per device."""
        if not self.manageable:
            return ()
        idle_after = self._settings.unload_idle_seconds
        unloaded: list[str] = []
        for entry in self.resident():
            if entry.model_id in in_use_model_ids:
                continue
            if (now - entry.last_used_at).total_seconds() >= idle_after and self._unload(
                entry, reason="idle", now=now
            ):
                unloaded.append(entry.canonical_id)
        return tuple(unloaded)

    def sync(self, now: datetime) -> None:
        """Reconcile the table with what the provider reports: close episodes it no longer holds.

        Models the provider reports that the table does not know are left alone — their device is
        unknown (ADR-0027: providers do not report placement) and a guessed ``gpu_index`` would be
        a fabricated measurement.
        """
        if not self.support.can_query:
            return
        try:
            reported = {
                resident.identity.provider_model_name for resident in self._provider.list_resident()
            }
        except ProviderError:
            return
        for entry in self.resident():
            if entry.provider_model_name not in reported:
                with self._database.write() as session:
                    row = session.get(Residency, entry.residency_id)
                    if row is not None:
                        row.resident = False
                        row.unloaded_at = now
                        row.unload_reason = "provider_reported_unloaded"
