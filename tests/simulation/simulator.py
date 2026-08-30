"""The scheduling simulator: a fake clock, a fake provider and a deterministic driver (queue §12).

**Why it exists.** The scheduler's interesting failures — a double execution under a lease race, a
job stuck in ``cancelling``, priority inversion, a starvation bound that holds only in prose — do
not show up in unit tests of the pieces and cannot be waited for in real time. This module runs the
*real* queue service, scheduler and worker code against a real (temporary) database, with every
source of time and every blocking call replaced by a discrete-event driver, so a five-hour
scheduling scenario runs in well under a second and produces the same event sequence every run.

**How time works.** Worker threads are real threads running the real worker loop, but they never
sleep on the wall clock: every place a worker would block — the provider's per-chunk delay, the
adaptive poll's wait, a retry backoff — is a *handshake* with the :class:`Driver`. The worker
registers when it next wants to run, signals the driver and blocks; the driver advances the
:class:`FakeClock` to the earliest pending instant and releases exactly one thread. Only one thread
ever runs at a time, and the order is fixed by ``(time, insertion order)``, so the interleaving of
two workers, the scheduler's ticks and the workload's arrivals is fully determined by the scenario.
The scheduler itself is not a thread here: the driver calls its ``tick`` at the configured cadence,
exactly as the production scheduler thread does on a real timer.

**What is fake and what is real.** Fake: time, the provider's work (a scripted generation whose
duration is simulated seconds), the GPU telemetry snapshot, and model placement. Real: the database
and its migrations, the queue service's statements, the worker's loop, admission, the lease keeper,
recovery, cancellation — every line of code the properties in queue §12 are about.
"""

from __future__ import annotations

import heapq
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from baseaicore import UNSUPPORTED, ModelDescriptor, ModelIdentity, ProviderKind, RuntimeProfile
from baseaicore.measurement import Measurement, is_supported
from modelrack import (
    GenerationRequest,
    GenerationResult,
    LoadResult,
    ProviderCapabilities,
    ProviderHealth,
    ResidentModel,
    StreamEvent,
)
from modelrack.testing import FakeFailure, FakeGeneration, FakeModel, FakeProvider, FakeScript
from sweatmeter import GpuSample, TelemetrySnapshot

from loadcoach.config import (
    ExecutionSettings,
    ProviderSettings,
    QueueSettings,
    ResidencySettings,
    Settings,
    StorageSettings,
    TelemetrySettings,
)
from loadcoach.domain.priority import JobClass
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.models import discover_models
from loadcoach.services.queue import EnqueueOutcome, JobSubmission, Wakeup, enqueue, get_job
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file
from loadcoach.services.worker import QueueRuntime, build_runtime

__all__ = [
    "DEFAULT_START",
    "SIMULATION_CAPABILITIES",
    "Driver",
    "FakeClock",
    "GenerationSpec",
    "GpuState",
    "SimulatedProvider",
    "SimulatedWakeup",
    "Simulation",
    "SimulationError",
    "SimulationStopped",
    "Wakeup",
    "sim_model",
    "sim_prompt",
]

DEFAULT_START: Final = datetime(2026, 8, 29, 8, 0, 0, tzinfo=UTC)
"""Where every simulation's clock starts unless a scenario says otherwise."""

SIMULATION_CAPABILITIES: Final = ProviderCapabilities(
    streaming=True,
    token_counts=True,
    json_mode=True,
    structured_output=True,
    context_configurable=True,
    force_unload=True,
    residency_query=True,
)
"""What the simulated provider declares: everything the queue's residency and admission paths need,
and ``token_level_chunks=False`` because the deltas are hand-placed."""

_REAL_TIMEOUT_SECONDS: Final = 30.0
"""How long the driver waits, in real time, for a released worker to reach its next handshake
before declaring the simulation deadlocked. A real database write takes milliseconds; anything
approaching this is a hang, not a slow test."""

_PROMPT_PREFIX: Final = "sim:"


class SimulationError(RuntimeError):
    """The simulation itself is broken — a worker never yielded, or was misused from a thread."""


class SimulationStopped(BaseException):
    """Raised inside a worker thread when the driver is tearing down, to unwind it.

    A ``BaseException`` rather than an ``Exception`` so the worker loop's ordinary failure handling
    (which catches ``Exception`` so one bad job cannot kill a worker) does not swallow it.
    """


class FakeClock:
    """A settable, thread-safe clock. Only the :class:`Driver` moves it; everyone else reads it."""

    def __init__(self, start: datetime = DEFAULT_START) -> None:
        """Start the clock at ``start``, which must be timezone-aware."""
        if start.tzinfo is None:
            message = "FakeClock needs an aware start instant"
            raise ValueError(message)
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> datetime:
        """Return the current simulated instant."""
        with self._lock:
            return self._now

    def set(self, instant: datetime) -> None:
        """Move the clock to ``instant``. Refuses to move backwards: simulated time is monotonic."""
        with self._lock:
            if instant < self._now:
                message = f"clock cannot move backwards: {instant} < {self._now}"
                raise SimulationError(message)
            self._now = instant

    def advance(self, seconds: float) -> datetime:
        """Move the clock forward by ``seconds`` and return the new instant."""
        with self._lock:
            self._now = self._now + timedelta(seconds=seconds)
            return self._now


@dataclass
class _Slot:
    """One registered worker thread and the two events that implement its handshake."""

    name: str
    turn: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    finished: bool = False
    error: BaseException | None = None
    release_token: int = 0
    woken: bool = False
    waiting: bool = False


@dataclass(frozen=True, slots=True)
class _Item:
    """One pending thing to do at an instant: an action, or a worker's release."""

    action: Callable[[], None] | None
    slot: _Slot | None
    token: int
    label: str


class Driver:
    """The discrete-event loop: owns the clock, the pending-event heap and the worker handshakes.

    Everything that would take time is an item on the heap: a workload arrival, a scheduler tick,
    or the instant a blocked worker is due to run again. :meth:`run_until` pops items in
    ``(time, insertion order)`` order, sets the clock, and either runs the action or releases the
    worker and waits for it to block again. Exactly one thread runs at a time.
    """

    def __init__(self, clock: FakeClock) -> None:
        """Create a driver over ``clock`` with nothing scheduled."""
        self.clock = clock
        self._heap: list[tuple[datetime, int, _Item]] = []
        self._sequence = 0
        self._lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}
        self._by_thread: dict[int, _Slot] = {}
        self._stopping = False
        self._log: list[tuple[datetime, str]] = []

    # ------------------------------------------------------------------ scheduling (any thread)

    def schedule(self, at: datetime, action: Callable[[], None], *, label: str = "") -> None:
        """Run ``action`` on the driver thread when the clock reaches ``at``.

        ``at`` in the past runs at the current instant, after everything already pending there.
        """
        self._push(max(at, self.clock.now()), _Item(action=action, slot=None, token=0, label=label))

    def every(
        self,
        interval_seconds: float,
        action: Callable[[], None],
        *,
        first_at: datetime | None = None,
        label: str = "",
    ) -> None:
        """Run ``action`` every ``interval_seconds``, starting at ``first_at`` (default: now)."""
        first = self.clock.now() if first_at is None else first_at

        def _run() -> None:
            action()
            self._push(
                self.clock.now() + timedelta(seconds=interval_seconds),
                _Item(action=_run, slot=None, token=0, label=label),
            )

        self._push(first, _Item(action=_run, slot=None, token=0, label=label))

    def _push(self, at: datetime, item: _Item) -> None:
        with self._lock:
            self._sequence += 1
            heapq.heappush(self._heap, (at, self._sequence, item))

    @property
    def log(self) -> tuple[tuple[datetime, str], ...]:
        """Every labelled item the driver dispatched, in order — the determinism witness."""
        return tuple(self._log)

    # ------------------------------------------------------------------------------- workers

    def add_worker(self, name: str, target: Callable[[], None]) -> None:
        """Start ``target`` on its own thread, blocked until the driver first releases it at now."""
        if name in self._slots:
            message = f"worker {name!r} already registered"
            raise SimulationError(message)
        slot = _Slot(name=name)
        thread = threading.Thread(
            target=self._run_worker, args=(slot, target), name=f"sim-{name}", daemon=True
        )
        slot.thread = thread
        self._slots[name] = slot
        self._by_thread[thread.ident or 0] = slot  # replaced below once the ident is known
        thread.start()
        self._by_thread.pop(0, None)
        self._by_thread[thread.ident or 0] = slot
        self._schedule_release(slot, self.clock.now())

    def _run_worker(self, slot: _Slot, target: Callable[[], None]) -> None:
        slot.turn.wait()
        slot.turn.clear()
        try:
            if not self._stopping:
                target()
        except SimulationStopped:
            pass
        except BaseException as exc:  # noqa: BLE001 — surfaced to the driver thread deliberately
            slot.error = exc
        finally:
            slot.finished = True
            slot.done.set()

    def _slot_for_current_thread(self) -> _Slot:
        slot = self._by_thread.get(threading.get_ident())
        if slot is None:
            message = (
                "a simulated blocking call was made from a thread the driver does not own "
                "(the driver thread itself, or an unregistered thread)"
            )
            raise SimulationError(message)
        return slot

    def _schedule_release(self, slot: _Slot, at: datetime) -> None:
        """Arrange for ``slot`` to run at ``at``, superseding any earlier arrangement."""
        slot.release_token += 1
        self._push(
            max(at, self.clock.now()),
            _Item(action=None, slot=slot, token=slot.release_token, label=f"release {slot.name}"),
        )

    def _block(self, slot: _Slot) -> None:
        """Hand control back to the driver and wait to be released. Worker thread only."""
        if self._stopping:
            raise SimulationStopped
        slot.done.set()
        slot.turn.wait()
        slot.turn.clear()
        if self._stopping:
            raise SimulationStopped

    def sleep(self, seconds: float) -> None:
        """Block the calling worker for ``seconds`` of simulated time. Worker thread only.

        This is what the simulated provider's per-chunk delay and a retry backoff call.
        """
        slot = self._slot_for_current_thread()
        self._schedule_release(slot, self.clock.now() + timedelta(seconds=max(seconds, 0.0)))
        self._block(slot)

    def _release(self, slot: _Slot) -> None:
        if slot.finished:
            return
        slot.done.clear()
        slot.turn.set()
        if not slot.done.wait(_REAL_TIMEOUT_SECONDS):
            message = f"worker {slot.name!r} did not reach its next handshake — deadlock?"
            raise SimulationError(message)
        if slot.error is not None:
            error, slot.error = slot.error, None
            raise error

    # -------------------------------------------------------------------------------- driving

    def run_until(self, until: datetime) -> None:
        """Dispatch every pending item due at or before ``until``, then set the clock to it."""
        while True:
            with self._lock:
                if not self._heap or self._heap[0][0] > until:
                    break
                at, _, item = heapq.heappop(self._heap)
            self.clock.set(at)
            if item.slot is not None:
                if item.token != item.slot.release_token:
                    continue  # superseded by a later arrangement (a wake-up, typically)
                self._log.append((at, item.label))
                self._release(item.slot)
            elif item.action is not None:
                if item.label:
                    self._log.append((at, item.label))
                item.action()
        if until > self.clock.now():
            self.clock.set(until)

    def run_for(self, seconds: float) -> None:
        """:meth:`run_until` ``seconds`` from now."""
        self.run_until(self.clock.now() + timedelta(seconds=seconds))

    def stop(self) -> None:
        """Unwind every worker thread and join it. Safe to call twice."""
        self._stopping = True
        for slot in self._slots.values():
            if slot.finished or slot.thread is None:
                continue
            slot.turn.set()
            slot.thread.join(_REAL_TIMEOUT_SECONDS)

    def workers_finished(self) -> bool:
        """Whether every registered worker thread has exited."""
        return all(slot.finished for slot in self._slots.values())


class SimulatedWakeup:
    """A :class:`Wakeup` whose ``wait`` is a driver handshake instead of a wall-clock sleep.

    Mirrors ``threading.Event``: ``wait`` returns ``True`` at once while the flag is set, otherwise
    blocks until ``set()`` or the timeout; ``set()`` wakes every current waiter *now* — their
    timeout release is superseded, exactly as a real event returns early.
    """

    def __init__(self, driver: Driver) -> None:
        """Create an unset wake-up bound to ``driver``."""
        self._driver = driver
        self._flag = False
        self._waiters: set[str] = set()
        self._lock = threading.Lock()

    def is_set(self) -> bool:
        """Whether the flag is set."""
        with self._lock:
            return self._flag

    def wait(self, timeout: float) -> bool:
        """Block the calling worker until set or until ``timeout`` simulated seconds elapse."""
        with self._lock:
            if self._flag:
                return True
        slot = self._driver._slot_for_current_thread()  # noqa: SLF001 — the driver's own harness
        slot.woken = False
        slot.waiting = True
        with self._lock:
            self._waiters.add(slot.name)
        self._driver._schedule_release(  # noqa: SLF001
            slot, self._driver.clock.now() + timedelta(seconds=max(timeout, 0.0))
        )
        try:
            self._driver._block(slot)  # noqa: SLF001
        finally:
            slot.waiting = False
            with self._lock:
                self._waiters.discard(slot.name)
        return slot.woken

    def set(self) -> None:
        """Set the flag and release every worker currently blocked in :meth:`wait`."""
        with self._lock:
            self._flag = True
            names = list(self._waiters)
        for name in names:
            slot = self._driver._slots[name]  # noqa: SLF001
            if slot.waiting and not slot.woken:
                slot.woken = True
                self._driver._schedule_release(slot, self._driver.clock.now())  # noqa: SLF001

    def clear(self) -> None:
        """Reset the flag."""
        with self._lock:
            self._flag = False


@dataclass(frozen=True, slots=True)
class GenerationSpec:
    """What one simulated generation does: its duration, its chunking, and whether it fails.

    Attributes:
        duration_seconds: Simulated wall time from the provider call to its terminal event, spread
            evenly over the chunks (the first delta arrives after one chunk's share).
        chunks: How many token deltas to produce.
        text: The complete text, or ``None`` for one word per chunk.
        failure: A scripted failure, delivered exactly as the fake delivers it (``after_chunks``
            counts deltas; ``None`` raises before the stream opens).
    """

    duration_seconds: float = 10.0
    chunks: int = 5
    text: str | None = None
    failure: FakeFailure | None = None


def sim_prompt(label: str) -> str:
    """Return the prompt text that makes the simulated provider look up ``label``'s spec."""
    return f"{_PROMPT_PREFIX}{label}"


def sim_model(
    name: str,
    *,
    size_bytes: int = 4 * 1024**3,
    load_seconds: float = 5.0,
    digest: str | None = None,
    **overrides: Any,
) -> FakeModel:
    """Build a catalogue entry with the geometry the VRAM estimator needs and a load time."""
    if digest is None:
        digest = (name.replace(":", "").replace("-", "").replace(".", "") * 64)[:64].ljust(64, "0")
    defaults: dict[str, Any] = {
        "family": name.split(":")[0],
        "parameter_count": 8_000_000_000,
        "quantization": "Q8_0",
        "size_bytes": size_bytes,
        "max_context": 8192,
        "layers": 32,
        "kv_heads": 8,
        "head_dim": 128,
        "vram_bytes": size_bytes,
        "load_ms": load_seconds * 1000.0,
    }
    defaults.update(overrides)
    return FakeModel(name=name, digest=digest, **defaults)


class SimulatedProvider:
    """A ModelRack provider whose generations take simulated time and whose residency is tracked.

    Catalogue, identity resolution and descriptors come from a real :class:`FakeProvider` over the
    same models, so discovery and routing see exactly what they would see from any fake. Each
    ``stream`` call builds a one-generation script from the spec its prompt names and runs it
    through a fresh fake whose ``sleep`` is the driver's handshake — so a 300-second generation
    costs no real time, interleaves with the scheduler's ticks and the other workers at every
    chunk boundary, and is cancellable within one chunk.

    Residency is modelled here rather than delegated: a model not resident when a generation
    starts is loaded first (``load_seconds`` of simulated time, counted in :attr:`loads`), exactly
    as a real runtime loads on demand, and :meth:`unload` evicts.
    """

    kind: ProviderKind = ProviderKind.FAKE

    def __init__(
        self,
        driver: Driver,
        *,
        models: Sequence[FakeModel],
        specs: Mapping[str, Sequence[GenerationSpec]] | None = None,
        default_spec: GenerationSpec | None = None,
        capabilities: ProviderCapabilities = SIMULATION_CAPABILITIES,
        seed: int = 0,
    ) -> None:
        """Create the provider over ``models``, with per-label generation scripts."""
        self._driver = driver
        self._models = tuple(models)
        self._capabilities = capabilities
        self._seed = seed
        self._base = FakeProvider(
            FakeScript(models=self._models, capabilities=capabilities),
            seed=seed,
            clock=driver.clock.now,
        )
        self._specs: dict[str, list[GenerationSpec]] = {
            label: list(sequence) for label, sequence in (specs or {}).items()
        }
        self._consumed: dict[str, int] = {}
        self._default_spec = default_spec or GenerationSpec()
        self._lock = threading.Lock()
        self._resident: dict[str, datetime] = {}
        self.loads = 0
        self.unloads = 0
        self.calls: list[tuple[datetime, str, str]] = []
        self.on_load: Callable[[str], None] | None = None
        self.on_unload: Callable[[str], None] | None = None

    # ---------------------------------------------------------------------- scenario control

    def script(self, label: str, *specs: GenerationSpec) -> None:
        """Set what successive generations for ``label`` do; the last spec repeats."""
        self._specs[label] = list(specs)
        self._consumed[label] = 0

    def _spec_for(self, label: str) -> GenerationSpec:
        with self._lock:
            sequence = self._specs.get(label)
            if not sequence:
                return self._default_spec
            index = self._consumed.get(label, 0)
            self._consumed[label] = index + 1
            return sequence[min(index, len(sequence) - 1)]

    @staticmethod
    def label_of(request: GenerationRequest) -> str:
        """Return the spec label named by the caller's first ``sim:`` message, else ``""``."""
        for message in request.messages:
            if message.content.startswith(_PROMPT_PREFIX):
                return message.content[len(_PROMPT_PREFIX) :]
        return ""

    def resident_names(self) -> frozenset[str]:
        """Which catalogue names are currently loaded."""
        with self._lock:
            return frozenset(self._resident)

    # -------------------------------------------------------------------- protocol: catalogue

    def health(self) -> ProviderHealth:
        """Delegate to the base fake."""
        return self._base.health()

    def capabilities(self) -> ProviderCapabilities:
        """Return the declared capabilities."""
        return self._capabilities

    def list_models(self, *, refresh: bool = False) -> Sequence[ModelDescriptor]:
        """Delegate to the base fake."""
        return self._base.list_models(refresh=refresh)

    def inspect_model(self, identity: ModelIdentity, *, refresh: bool = False) -> ModelDescriptor:
        """Delegate to the base fake."""
        return self._base.inspect_model(identity, refresh=refresh)

    def resolve(self, reference: str, *, refresh: bool = False) -> ModelIdentity:
        """Delegate to the base fake."""
        return self._base.resolve(reference, refresh=refresh)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Blocking generation is never used by LoadCoach (it always streams); delegate anyway."""
        return self._base.generate(request)

    # ------------------------------------------------------------------- protocol: generation

    def stream(self, request: GenerationRequest) -> Iterator[StreamEvent]:
        """Run the scripted generation for this request's label, in simulated time."""
        label = self.label_of(request)
        spec = self._spec_for(label)
        name = request.identity.provider_model_name
        self.calls.append((self._driver.clock.now(), name, label))
        self._ensure_loaded(name)
        per_chunk_ms = (spec.duration_seconds * 1000.0) / max(spec.chunks, 1)
        chunks = (
            tuple(f"{label or 'token'}-{index} " for index in range(spec.chunks))
            if spec.text is None
            else _split(spec.text, spec.chunks)
        )
        generation = FakeGeneration(
            chunks=chunks,
            first_chunk_delay_ms=per_chunk_ms,
            chunk_delay_ms=per_chunk_ms,
            failure=spec.failure,
        )
        fake = FakeProvider(
            FakeScript(
                models=self._models, capabilities=self._capabilities, generations=(generation,)
            ),
            seed=self._seed,
            sleep=self._driver.sleep,
            clock=self._driver.clock.now,
        )
        yield from fake.stream(request)

    def _ensure_loaded(self, name: str) -> None:
        with self._lock:
            already = name in self._resident
        if already:
            with self._lock:
                self._resident[name] = self._driver.clock.now()
            return
        model = next(candidate for candidate in self._models if candidate.name == name)
        load_ms: Measurement = model.load_ms
        seconds = float(load_ms) / 1000.0 if is_supported(load_ms) else 0.0
        if seconds > 0:
            self._driver.sleep(seconds)
        with self._lock:
            self._resident[name] = self._driver.clock.now()
            self.loads += 1
        on_load = self.on_load
        if on_load is not None:
            on_load(name)

    # -------------------------------------------------------------------- protocol: residency

    def load(self, identity: ModelIdentity, profile: RuntimeProfile) -> LoadResult:
        """Load ``identity`` on demand, spending its scripted load time."""
        name = identity.provider_model_name
        already = name in self.resident_names()
        self._ensure_loaded(name)
        return LoadResult(
            identity=identity,
            already_resident=already,
            load_ms=UNSUPPORTED,
            profile_hash=profile.profile_hash,
        )

    def unload(self, identity: ModelIdentity) -> bool:
        """Evict ``identity``; return whether it was resident."""
        name = identity.provider_model_name
        with self._lock:
            was = self._resident.pop(name, None) is not None
            if was:
                self.unloads += 1
        on_unload = self.on_unload
        if was and on_unload is not None:
            on_unload(name)
        return was

    def list_resident(self) -> Sequence[ResidentModel]:
        """Report what is loaded, with the catalogue's ``vram_bytes``."""
        with self._lock:
            names = sorted(self._resident)
        by_name = {model.name: model for model in self._models}
        return tuple(
            ResidentModel(
                identity=self._base.resolve(name),
                vram_bytes=by_name[name].vram_bytes,
                total_bytes=UNSUPPORTED,
            )
            for name in names
        )


def _split(text: str, chunks: int) -> tuple[str, ...]:
    """Cut ``text`` into ``chunks`` pieces, the last taking the remainder."""
    count = max(chunks, 1)
    size = max(len(text) // count, 1)
    pieces = [text[i * size : (i + 1) * size] for i in range(count - 1)]
    pieces.append(text[(count - 1) * size :])
    return tuple(piece for piece in pieces if piece) or (text,)


@dataclass
class GpuState:
    """One simulated device's memory, mutated as models load and unload."""

    index: int
    total_bytes: int
    used_bytes: int = 0

    @property
    def free_bytes(self) -> int:
        """Total minus used, never negative."""
        return max(self.total_bytes - self.used_bytes, 0)


class Simulation:
    """A scenario's shared infrastructure: database, clock, driver, provider, GPUs and settings.

    Built once per scenario. The scheduler and the workers are attached by the property tests
    once they exist (units 3 and 4 of the phase), through :meth:`attach_scheduler` and
    :meth:`add_worker`; this class knows nothing about them beyond the callables it drives.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        models: Sequence[FakeModel] | None = None,
        gpus: Sequence[tuple[int, int]] = ((0, 16 * 1024**3),),
        start: datetime = DEFAULT_START,
        queue: QueueSettings | None = None,
        execution: ExecutionSettings | None = None,
        residency: ResidencySettings | None = None,
        telemetry: TelemetrySettings | None = None,
        specs: Mapping[str, Sequence[GenerationSpec]] | None = None,
        default_spec: GenerationSpec | None = None,
        placement: Mapping[str, int] | None = None,
    ) -> None:
        """Create the temporary database, discover ``models`` and build the simulated machine.

        Args:
            tmp_path: Where the SQLite file lives.
            models: The catalogue. Default: one 4 GiB model, ``alpha:8b``.
            gpus: ``(index, total_bytes)`` per device.
            start: The clock's first instant.
            queue: Queue settings; default is the shipped defaults.
            execution: Execution settings; default is the shipped defaults.
            residency: Residency settings; default is the shipped defaults.
            telemetry: Telemetry settings (the per-device headroom); default is the shipped one.
            specs: Per-label generation scripts.
            default_spec: What an unscripted label does.
            placement: Which device each model lands on when loaded (default: 0). Placement is
                a simulation input because ModelRack's ``load`` names no device (ADR-0027).
        """
        self.start = start
        self.clock = FakeClock(start)
        self.driver = Driver(self.clock)
        self.models = tuple(models) if models is not None else (sim_model("alpha:8b"),)
        self.gpus: dict[int, GpuState] = {
            index: GpuState(index=index, total_bytes=total) for index, total in gpus
        }
        self._placement = dict(placement or {})
        self.settings = Settings(
            storage=StorageSettings(database_url=f"sqlite:///{tmp_path / 'simulation.sqlite3'}"),
            provider=ProviderSettings(kind="fake"),
            queue=queue or QueueSettings(),
            execution=execution or ExecutionSettings(),
            residency=residency or ResidencySettings(),
            telemetry=telemetry or TelemetrySettings(),
        )
        self.provider = SimulatedProvider(
            self.driver, models=self.models, specs=specs, default_spec=default_spec
        )
        self.provider.on_load = self._on_load
        self.provider.on_unload = self._on_unload
        self.wakeup = SimulatedWakeup(self.driver)
        self.sink = JobEventSink()
        self.runtime: QueueRuntime | None = None
        url = self.settings.storage.database_url
        assert url is not None
        self.database = Database.from_url(url)
        ensure_ready(self.database, auto_migrate=True)
        import_task_profiles(self.database, read_task_profiles_file(), now=self.clock.now())
        discover_models(self.database, self.provider, now=self.clock.now())

    # ------------------------------------------------------------------------------ machine

    def _device_for(self, name: str) -> GpuState:
        index = self._placement.get(name, min(self.gpus) if self.gpus else 0)
        return self.gpus[index]

    def _vram_of(self, name: str) -> int:
        model = next(candidate for candidate in self.models if candidate.name == name)
        value: Measurement = model.vram_bytes
        return int(value) if is_supported(value) else 0

    def _on_load(self, name: str) -> None:
        if self.gpus:
            self._device_for(name).used_bytes += self._vram_of(name)

    def _on_unload(self, name: str) -> None:
        if self.gpus:
            device = self._device_for(name)
            device.used_bytes = max(device.used_bytes - self._vram_of(name), 0)

    def snapshot(self) -> TelemetrySnapshot:
        """The telemetry admission reads: one sample per simulated device, as of now."""
        return TelemetrySnapshot(
            timestamp=self.clock.now(),
            gpus=tuple(
                GpuSample(
                    index=device.index,
                    vram_total_bytes=device.total_bytes,
                    vram_used_bytes=device.used_bytes,
                )
                for device in sorted(self.gpus.values(), key=lambda d: d.index)
            ),
        )

    def occupy(self, gpu_index: int, bytes_used: int) -> None:
        """Simulate memory held by something other than a model (another process, a display)."""
        self.gpus[gpu_index].used_bytes = bytes_used

    # ---------------------------------------------------------------------------- submitting

    def submit(
        self,
        label: str,
        *,
        task: str = "general.chat",
        job_class: JobClass = JobClass.NORMAL,
        priority: int | None = None,
        max_wait_seconds: int | None = None,
        idempotent: bool = True,
        idempotency_key: str | None = None,
        source: str = "simulation",
        model: str | None = None,
        stream: bool = False,
    ) -> EnqueueOutcome:
        """Enqueue a job whose generation follows ``label``'s script, through the real service."""
        from loadcoach.domain.routing.subject import RuntimeOverrides

        submission = JobSubmission(
            task=task,
            prompt=sim_prompt(label),
            job_class=job_class,
            priority=priority,
            max_wait_seconds=max_wait_seconds,
            idempotent=idempotent,
            idempotency_key=idempotency_key,
            source=source,
            overrides=None if model is None else RuntimeOverrides(model=model),
            stream=stream,
        )
        return enqueue(
            self.database,
            submission,
            now=self.clock.now(),
            queue_settings=self.settings.queue,
            execution_settings=self.settings.execution,
            sink=self.sink,
            wakeup=self.wakeup,
        )

    # ------------------------------------------------------------------------- the real queue

    def start_queue(
        self, *, workers: int | None = None, tick_seconds: float = 0.25
    ) -> QueueRuntime:
        """Build the real runtime over this simulation's primitives and start driving it.

        The workers run the real :class:`~loadcoach.services.worker.Worker` loop on simulated
        threads; the real :class:`~loadcoach.services.worker.Scheduler`'s ``tick`` is called
        every ``tick_seconds`` of simulated time, exactly as the production thread calls it on a
        real timer.
        """
        runtime = build_runtime(
            self.settings,
            database=self.database,
            provider=self.provider,
            sink=self.sink,
            snapshot=self.snapshot,
            clock=self.clock.now,
            wakeup=self.wakeup,
            sleep=self.driver.sleep,
            workers=workers,
            owner_prefix="sim",
        )
        self.runtime = runtime
        for worker in runtime.workers:
            self.add_worker(worker.worker_id, worker.run)
        assert runtime.scheduler is not None
        self.attach_scheduler(runtime.scheduler.tick, interval_seconds=tick_seconds)
        return runtime

    def job(self, job_id: str) -> Any:
        """Read one job's record."""
        return get_job(self.database, job_id)

    def events(self, job_id: str) -> list[tuple[int, str]]:
        """The persisted event stream of one job as ``(sequence, type)`` pairs."""
        from sqlalchemy import select

        from loadcoach.infrastructure.db.models import JobEvent

        with self.database.read() as session:
            return [
                (row.sequence, row.event_type)
                for row in session.execute(
                    select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.sequence)
                ).scalars()
            ]

    def attempts(self, job_id: str) -> list[tuple[int, str]]:
        """The persisted attempts of one job as ``(attempt, outcome)`` pairs."""
        from sqlalchemy import select

        from loadcoach.infrastructure.db.models import JobAttempt

        with self.database.read() as session:
            return [
                (row.attempt, row.outcome)
                for row in session.execute(
                    select(JobAttempt)
                    .where(JobAttempt.job_id == job_id)
                    .order_by(JobAttempt.attempt)
                ).scalars()
            ]

    # ------------------------------------------------------------------------------- driving

    def attach_scheduler(
        self, tick: Callable[[datetime], None], *, interval_seconds: float
    ) -> None:
        """Run ``tick(now)`` every ``interval_seconds`` of simulated time, starting now."""
        self.driver.every(interval_seconds, lambda: tick(self.clock.now()), label="tick")

    def add_worker(self, name: str, target: Callable[[], None]) -> None:
        """Run ``target`` as a simulated worker thread."""
        self.driver.add_worker(name, target)

    def run_for(self, seconds: float) -> None:
        """Advance the scenario by ``seconds``."""
        self.driver.run_for(seconds)

    def run_until(self, until: datetime) -> None:
        """Advance the scenario to ``until``."""
        self.driver.run_until(until)

    def at(self, seconds_from_start: float, action: Callable[[], None], *, label: str = "") -> None:
        """Schedule ``action`` at ``start + seconds_from_start``; a past instant means now."""
        self.driver.schedule(
            self.start + timedelta(seconds=seconds_from_start), action, label=label
        )

    def close(self) -> None:
        """Unwind the workers and release the database."""
        if self.runtime is not None:
            for worker in self.runtime.workers:
                worker.stop()
        self.driver.stop()
        self.database.close()
