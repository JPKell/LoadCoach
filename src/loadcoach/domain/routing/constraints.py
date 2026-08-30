"""loadcoach.domain.routing.constraints — the VRAM/KV estimator and the hard constraint filter.

Two things live here, in the order routing needs them.

**The estimator** is the pure function [Queue §5](../../apps/loadcoach/queue-and-scheduling.md)
specifies, brought forward from P5 because the ``insufficient_vram`` constraint cannot be written
without it. It takes a model, a resolved runtime profile and a telemetry snapshot, and returns
numbers. It contains **no admission policy**: nothing here defers a job, moves it to
``waiting_resources``, re-evaluates it when a model unloads, or sums concurrent jobs against a
device. That is P5's, and building it here would be building the wrong thing twice.

**The filter** applies routing §4's ten hard constraints in the documented order, stopping at the
first failure and recording it with the numbers that caused it — because "nothing was eligible" is
useless without them. Devices are evaluated independently and never summed (ADR-0027 §2): a model
larger than either of two devices but smaller than their sum does not fit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from baseaicore import is_supported

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sweatmeter import TelemetrySnapshot

    from loadcoach.domain.routing.subject import ExecutionSubject

__all__ = [
    "ACTIVATION_OVERHEAD_BYTES",
    "ASSUMED_KV_CACHE_PRECISION",
    "DEFAULT_VRAM_HEADROOM_BYTES",
    "LOADING_OVERHEAD_FACTOR",
    "ConstraintInputs",
    "DeviceFit",
    "Rejection",
    "VramEstimate",
    "device_fits",
    "estimate_vram",
    "evaluate_constraints",
    "free_vram_by_gpu",
    "kv_bytes_per_token",
    "sortable_estimate",
]

LOADING_OVERHEAD_FACTOR: Final = 1.05
"""Weights occupy more than their file size once loaded: allocator padding, tensor metadata and
the runtime's own buffers. 5% is a deliberately small, documented constant rather than a tuned
figure — the KV term dominates at any interesting context length, and a larger fudge here would
disguise how much of the estimate is actually arithmetic."""

ACTIVATION_OVERHEAD_BYTES: Final = 256 * 1024 * 1024
"""Per-execution scratch: activations, the compute graph and the runtime's working buffers.
Independent of context length to first order at batch size 1, which is what LoadCoach schedules."""

ASSUMED_KV_CACHE_PRECISION: Final = "f16"
"""What a KV cache is assumed to be stored at when the resolved profile does not say.

Every mainstream local runtime defaults to 16-bit, and assuming it is the conservative direction:
a cache actually kept at ``q8_0`` or ``q4_0`` uses *less* than this estimate, so the error admits
no work that would OOM. An estimate built on this assumption records
``kv_precision_assumed=True``, so a decision can never present it as a measurement."""

DEFAULT_VRAM_HEADROOM_BYTES: Final = 512 * 1024 * 1024
"""Fallback for ``[telemetry].vram_headroom_bytes``, kept here so the pure function has a
defensible default without importing configuration."""

_BYTES_PER_ELEMENT: Final[Mapping[str, float]] = {
    "f32": 4.0,
    "fp32": 4.0,
    "f16": 2.0,
    "fp16": 2.0,
    "bf16": 2.0,
    "q8_0": 1.0,
    "q8": 1.0,
    "q5_1": 0.625,
    "q5_0": 0.625,
    "q4_0": 0.5,
    "q4_1": 0.5,
    "q4": 0.5,
}


@dataclass(frozen=True, slots=True)
class VramEstimate:
    """What one candidate is estimated to need on one device, and how that number was reached.

    ``total_bytes`` is ``None`` — never ``0`` — when a required input was never reported. An
    unknown estimate is a different claim from a zero one, and the two must not be confused
    (ADR-0016).

    Attributes:
        total_bytes: The complete estimate, or ``None`` if it cannot be produced.
        weights_bytes: ``size_bytes × LOADING_OVERHEAD_FACTOR``.
        kv_bytes: ``kv_bytes_per_token × served_context``.
        activation_bytes: The fixed scratch term.
        kv_bytes_per_token: The per-token figure used, or ``None``.
        kv_source: ``"observed"`` (FreeWeight measured it), ``"theoretical"`` (computed from the
            descriptor's geometry) or ``"unknown"``.
        kv_precision_assumed: Whether :data:`ASSUMED_KV_CACHE_PRECISION` was substituted.
        served_context: The context the KV term multiplied — never the advertised maximum.
        unknown_reason: Which input was missing, when ``total_bytes`` is ``None``.
    """

    total_bytes: int | None
    weights_bytes: int | None
    kv_bytes: int | None
    activation_bytes: int
    kv_bytes_per_token: float | None
    kv_source: str
    kv_precision_assumed: bool
    served_context: int
    unknown_reason: str | None = None

    def as_json(self) -> dict[str, object]:
        """Return this estimate as the plain mapping an explanation and its storage carry."""
        return {
            "total_bytes": self.total_bytes,
            "weights_bytes": self.weights_bytes,
            "kv_bytes": self.kv_bytes,
            "activation_bytes": self.activation_bytes,
            "kv_bytes_per_token": self.kv_bytes_per_token,
            "kv_source": self.kv_source,
            "kv_precision_assumed": self.kv_precision_assumed,
            "served_context": self.served_context,
            "unknown_reason": self.unknown_reason,
        }


@dataclass(frozen=True, slots=True)
class DeviceFit:
    """Whether one GPU can hold one candidate, with the numbers behind the verdict."""

    gpu_index: int
    free_bytes: int | None
    required_bytes: int | None
    headroom_bytes: int
    fits: bool


def kv_bytes_per_token(
    *,
    layers: int | None,
    kv_heads: int | None,
    head_dim: int | None,
    kv_cache_precision: str | None,
    observed: float | None = None,
) -> tuple[float | None, str, bool]:
    """Return the KV-cache cost of one token, its source, and whether precision was assumed.

    FreeWeight's measured ``observed_kv_bytes_per_token`` wins whenever it exists: it is a
    measurement of the runtime that will actually serve the request, and the theoretical figure is
    only ever an upper-bound model of it (queue §5).

    The theoretical figure is ``2 × layers × kv_heads × head_dim × bytes_per_element`` — one key
    tensor and one value tensor per layer.

    Args:
        layers: Transformer block count.
        kv_heads: Key/value head count (grouped-query models report fewer than attention heads).
        head_dim: Per-head dimension.
        kv_cache_precision: The resolved profile's setting; ``None`` means the provider decides,
            and :data:`ASSUMED_KV_CACHE_PRECISION` is substituted.
        observed: FreeWeight's measured figure, when evidence for this subject exists.

    Returns:
        ``(bytes_per_token, source, precision_assumed)``. ``bytes_per_token`` is ``None`` when the
        descriptor never reported the geometry and nothing measured it — an absence, not a zero.
    """
    if observed is not None:
        return observed, "observed", False
    if layers is None or kv_heads is None or head_dim is None:
        return None, "unknown", False
    assumed = kv_cache_precision is None
    precision = (kv_cache_precision or ASSUMED_KV_CACHE_PRECISION).strip().lower()
    element_bytes = _BYTES_PER_ELEMENT.get(precision)
    if element_bytes is None:
        return None, "unknown", False
    return 2.0 * layers * kv_heads * head_dim * element_bytes, "theoretical", assumed


def estimate_vram(
    *,
    size_bytes: int | None,
    served_context: int,
    layers: int | None = None,
    kv_heads: int | None = None,
    head_dim: int | None = None,
    kv_cache_precision: str | None = None,
    observed_kv_bytes_per_token: float | None = None,
    loading_overhead_factor: float = LOADING_OVERHEAD_FACTOR,
    activation_overhead_bytes: int = ACTIVATION_OVERHEAD_BYTES,
) -> VramEstimate:
    """Estimate what one model under one runtime profile needs on a single device.

    ``estimate_vram = size_bytes × loading_overhead_factor + kv_bytes_per_token × served_context
    + activation_overhead`` (queue §5). ``served_context`` — never the descriptor's advertised
    maximum — is what the KV term multiplies (ADR-0023 §4): estimating KV for a 131 072-token
    context the provider will never serve rejects every candidate, and estimating it for 4 096
    when 32 768 is served produces the OOM the estimate promised could not happen.

    Pure. It reads no telemetry and makes no admission decision; :func:`device_fits` compares its
    output against what a device actually has free.

    Args:
        size_bytes: On-disk weight size. ``None`` makes the whole estimate unknown.
        served_context: The resolved served context, in tokens.
        layers: Transformer block count, for the theoretical KV figure.
        kv_heads: Key/value head count.
        head_dim: Per-head dimension.
        kv_cache_precision: The resolved profile's KV precision, or ``None`` for provider default.
        observed_kv_bytes_per_token: FreeWeight's measured figure, when it exists.
        loading_overhead_factor: Override for :data:`LOADING_OVERHEAD_FACTOR`.
        activation_overhead_bytes: Override for :data:`ACTIVATION_OVERHEAD_BYTES`.

    Returns:
        The estimate, with ``total_bytes=None`` and a named ``unknown_reason`` when an input the
        arithmetic needs was never reported.
    """
    per_token, kv_source, assumed = kv_bytes_per_token(
        layers=layers,
        kv_heads=kv_heads,
        head_dim=head_dim,
        kv_cache_precision=kv_cache_precision,
        observed=observed_kv_bytes_per_token,
    )
    weights = None if size_bytes is None else int(size_bytes * loading_overhead_factor)
    kv = None if per_token is None else int(per_token * served_context)

    reason: str | None = None
    if size_bytes is None and per_token is None:
        reason = "the provider reported neither a weight size nor the geometry a KV figure needs"
    elif size_bytes is None:
        reason = "the provider reported no weight size for this model"
    elif per_token is None:
        reason = (
            "no measured KV figure exists and the provider reported no layers/kv_heads/head_dim "
            "to compute a theoretical one"
        )

    total = None if weights is None or kv is None else weights + kv + activation_overhead_bytes
    return VramEstimate(
        total_bytes=total,
        weights_bytes=weights,
        kv_bytes=kv,
        activation_bytes=activation_overhead_bytes,
        kv_bytes_per_token=per_token,
        kv_source=kv_source,
        kv_precision_assumed=assumed,
        served_context=served_context,
        unknown_reason=reason,
    )


def free_vram_by_gpu(snapshot: TelemetrySnapshot) -> dict[int, int | None]:
    """Return free VRAM per visible device, ``None`` where the driver did not report it.

    Free is ``total - used``; a device that reported only one of the two reports neither, because
    the difference of a number and an absence is an absence (ADR-0016), not the number.

    Args:
        snapshot: One telemetry observation.

    Returns:
        ``gpu_index -> free bytes or None``, one entry per visible device.
    """
    free: dict[int, int | None] = {}
    for gpu in snapshot.gpus:
        total = gpu.vram_total_bytes
        used = gpu.vram_used_bytes
        if is_supported(total) and is_supported(used):
            free[gpu.index] = max(int(total) - int(used), 0)
        else:
            free[gpu.index] = None
    return free


def device_fits(
    estimate: VramEstimate, snapshot: TelemetrySnapshot, *, headroom_bytes: int
) -> tuple[DeviceFit, ...]:
    """Evaluate every visible device independently against ``estimate`` (ADR-0027 §2).

    Devices are **never summed**: weights land on one device unless the runtime is explicitly told
    to shard, so a machine with 8 GB free on each of two GPUs cannot run a 14 GB model. Adding the
    two figures would admit work that OOMs.

    An unknown estimate, or a device whose free VRAM was never reported, does not fit. Admission
    never guesses optimistically (queue §5): a "maybe" that turns out to be a "no" is an OOM, and
    a "maybe" recorded as a "no" is a deferral.

    Args:
        estimate: What the candidate needs.
        snapshot: What the machine currently has.
        headroom_bytes: Per-device reserve, kept free rather than allocated.

    Returns:
        One :class:`DeviceFit` per visible device, in ascending device order. Empty when the
        machine reports no GPU at all — a CPU-only machine, where this constraint does not apply.
    """
    required = estimate.total_bytes
    fits: list[DeviceFit] = []
    for index, free in sorted(free_vram_by_gpu(snapshot).items()):
        ok = required is not None and free is not None and required + headroom_bytes <= free
        fits.append(
            DeviceFit(
                gpu_index=index,
                free_bytes=free,
                required_bytes=required,
                headroom_bytes=headroom_bytes,
                fits=ok,
            )
        )
    return tuple(fits)


@dataclass(frozen=True, slots=True)
class Rejection:
    """Why one candidate was removed, with the numbers that caused it (routing §4)."""

    reason: str
    detail: dict[str, object]


@dataclass(frozen=True, slots=True)
class ConstraintInputs:
    """Everything routing §4's ten constraints read, injected rather than fetched.

    Attributes:
        min_context_tokens: The profile's context requirement; 0 means none.
        requires_capabilities: Capabilities the provider must support.
        min_capability_scores: Per-capability floors, applied to resolved scores.
        exclude_models: Canonical IDs the profile refuses.
        allow_remote_providers: Whether a remote provider may be routed to at all.
        required_context: What context budgeting says this request needs, or ``None`` when the
            caller supplied no size to budget against.
        resolved_scores: Capability -> resolved score, or ``None`` for an absent one. An absent
            score is never treated as below a floor: absence of evidence is not evidence of
            incapacity (routing §5).
        snapshot: The telemetry the VRAM and RAM constraints read.
        vram_headroom_bytes: Per-device reserve (ADR-0027 §2).
        open_circuit_breakers: Canonical IDs currently excluded by the breaker. Populated by
            Phase 5's breaker over ``job_attempts`` outcomes; P7 drives it from
            ``reliability_stats``.
        resident_devices: Canonical ID -> devices the model is resident on. A resident model
            fits on its device whatever the estimate says — its memory is already allocated —
            which is the one exception to "an unknown estimate does not fit" (queue §5).
    """

    min_context_tokens: int = 0
    requires_capabilities: tuple[str, ...] = ()
    min_capability_scores: Mapping[str, float] | None = None
    exclude_models: tuple[str, ...] = ()
    allow_remote_providers: bool = False
    required_context: int | None = None
    resolved_scores: Mapping[str, float | None] | None = None
    snapshot: TelemetrySnapshot | None = None
    vram_headroom_bytes: int = DEFAULT_VRAM_HEADROOM_BYTES
    open_circuit_breakers: frozenset[str] = frozenset()
    resident_devices: Mapping[str, frozenset[int]] = field(default_factory=dict)


_CAPABILITY_SUPPORT_ATTRIBUTE: Final[Mapping[str, str]] = {
    "tool_use": "supports_tool_use",
    "structured_output": "supports_structured_output",
}


def evaluate_constraints(
    subject: ExecutionSubject, estimate: VramEstimate, inputs: ConstraintInputs
) -> tuple[Rejection | None, tuple[DeviceFit, ...], int | None]:
    """Apply routing §4's hard constraints in order, stopping at the first failure.

    Args:
        subject: The candidate, with its resolved profile and served context.
        estimate: This candidate's VRAM estimate, from :func:`estimate_vram`.
        inputs: The profile's constraints and the machine state they are evaluated against.

    Returns:
        ``(rejection, device_fits, target_gpu_index)``. ``rejection`` is ``None`` when the
        candidate survives. ``target_gpu_index`` is the device that satisfied the VRAM check, or
        ``None`` on a machine with no GPU (where the check does not apply) or when the candidate
        was rejected first.
    """
    facts = subject.facts
    served = subject.served_context

    if not facts.available or not subject.provider.healthy:
        return (
            Rejection(
                "model_unavailable",
                {
                    "available": facts.available,
                    "provider_healthy": subject.provider.healthy,
                    "reason": facts.unavailable_reason,
                },
            ),
            (),
            None,
        )

    if inputs.min_context_tokens > 0 and served.tokens < inputs.min_context_tokens:
        return (
            Rejection(
                "context_too_small",
                {
                    "served_context": served.tokens,
                    "served_context_source": served.source,
                    "min_context_tokens": inputs.min_context_tokens,
                    "advertised_max_context": facts.max_context,
                },
            ),
            (),
            None,
        )

    requested_context = subject.runtime_profile.context_size
    if (
        requested_context is not None
        and not subject.provider.context_configurable
        and requested_context != served.tokens
    ):
        return (
            Rejection(
                "context_not_configurable",
                {
                    "requested_context": requested_context,
                    "served_context": served.tokens,
                    "served_context_source": served.source,
                    "min_context_tokens": inputs.min_context_tokens,
                    "context_configurable": False,
                },
            ),
            (),
            None,
        )

    if inputs.required_context is not None and inputs.required_context > served.tokens:
        return (
            Rejection(
                "context_limit_exceeded",
                {
                    "required_context": inputs.required_context,
                    "served_context": served.tokens,
                    "served_context_source": served.source,
                },
            ),
            (),
            None,
        )

    for capability in inputs.requires_capabilities:
        attribute = _CAPABILITY_SUPPORT_ATTRIBUTE.get(capability)
        if attribute is not None and not getattr(subject.provider, attribute):
            return (
                Rejection(
                    "capability_unsupported",
                    {"capability": capability, "provider_kind": facts.provider_kind},
                ),
                (),
                None,
            )

    fits: tuple[DeviceFit, ...] = ()
    target_gpu_index: int | None = None
    if inputs.snapshot is not None:
        fits = device_fits(estimate, inputs.snapshot, headroom_bytes=inputs.vram_headroom_bytes)
        if fits:
            resident_on = inputs.resident_devices.get(facts.canonical_id, frozenset())
            resident_here = [fit for fit in fits if fit.gpu_index in resident_on]
            satisfying = [fit for fit in fits if fit.fits]
            if resident_here:
                # Already loaded there: its memory is allocated, so the device holds it whatever
                # the estimate says, and choosing another device would load a second copy.
                target_gpu_index = resident_here[0].gpu_index
            elif not satisfying:
                return (
                    Rejection(
                        "insufficient_vram",
                        {
                            "estimated_bytes": estimate.total_bytes,
                            "free_bytes_by_gpu": {
                                str(fit.gpu_index): fit.free_bytes for fit in fits
                            },
                            "headroom_bytes": inputs.vram_headroom_bytes,
                            "estimate": estimate.as_json(),
                        },
                    ),
                    fits,
                    None,
                )
            else:
                target_gpu_index = satisfying[0].gpu_index
        else:
            ram_rejection = _check_host_ram(estimate, inputs.snapshot)
            if ram_rejection is not None:
                return ram_rejection, fits, None

    if inputs.min_capability_scores:
        scores = inputs.resolved_scores or {}
        for capability, floor in sorted(inputs.min_capability_scores.items()):
            score = scores.get(capability)
            if score is not None and score < floor:
                return (
                    Rejection(
                        "below_minimum_score",
                        {"capability": capability, "score": score, "minimum": floor},
                    ),
                    fits,
                    None,
                )

    if facts.canonical_id in inputs.exclude_models:
        return (
            Rejection("excluded_by_policy", {"rule": "exclude_models"}),
            fits,
            None,
        )
    if facts.is_remote and not inputs.allow_remote_providers:
        return (
            Rejection(
                "excluded_by_policy",
                {"rule": "allow_remote_providers", "provider_kind": facts.provider_kind},
            ),
            fits,
            None,
        )

    if facts.canonical_id in inputs.open_circuit_breakers:
        return (Rejection("recently_failing", {"circuit_breaker": "open"}), fits, None)

    return None, fits, target_gpu_index


def _check_host_ram(estimate: VramEstimate, snapshot: TelemetrySnapshot) -> Rejection | None:
    """Reject a candidate that cannot fit in host RAM on a machine with no GPU.

    Evaluated only when no GPU is visible, because that is the only case in which this estimate
    describes host memory: once a device has been found to hold the weights, what the host still
    needs is page cache the kernel reclaims under pressure, and this function has no honest way to
    bound it. Reporting a number it cannot derive would be the fabricated-measurement failure the
    suite refuses everywhere else.
    """
    required = estimate.total_bytes
    available = snapshot.ram_available_bytes
    if required is None or not is_supported(available):
        return None
    if required > int(available):
        return Rejection(
            "insufficient_ram",
            {"estimated_bytes": required, "free_bytes": int(available)},
        )
    return None


def sortable_estimate(estimate: VramEstimate) -> float:
    """Return ``total_bytes`` as a sort key, with an unknown estimate ordering last."""
    return math.inf if estimate.total_bytes is None else float(estimate.total_bytes)
