"""loadcoach.infrastructure.providers.factory — the one place a ``Provider`` is constructed.

Coding standards §5: "Every application has one composition root where concretions are built.
Nothing else calls a constructor for infrastructure." For a model provider, that root is this
function — called from :mod:`loadcoach.web.app`'s lifespan for the running server and from the
health service for a one-shot CLI invocation — never from ``services/`` or ``domain/`` directly.

Not in the Phase 1 file list verbatim, but required by it: acceptance criterion 1
("``loadcoach serve`` ... reports degraded health with no provider") needs a provider to report
degraded health *about*, and this is the only place one is built.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from baseaicore import ConfigurationError

if TYPE_CHECKING:
    from modelrack.provider import Provider
    from modelrack.testing import FakeModel

    from loadcoach.config import (
        FakeProviderSettings,
        ProviderRegistrationSettings,
        ProviderSettings,
        Settings,
    )

__all__ = [
    "SUPPORTED_PROVIDER_KINDS",
    "ProviderRegistration",
    "build_provider",
    "build_registrations",
]

SUPPORTED_PROVIDER_KINDS: frozenset[str] = frozenset({"ollama", "llamacpp", "fake"})
"""``provider.kind`` values this phase can construct.

``"ollama"`` is the production adapter (spec §5: "a model provider (Ollama by default)").
``"fake"`` constructs :class:`~modelrack.testing.FakeProvider` so the running application — not
just its unit tests — can be exercised with no GPU, no Ollama and no network.
``"llamacpp"`` constructs :class:`~modelrack.providers.llamacpp.LlamaCppProvider`, which launches
and supervises its own server over a directory of GGUF weights — the one provider kind that can
hot-swap adapters (ADR-0062), and therefore the only kind an adapter subject can ever be served
by. ``openai_compatible`` and ``vllm`` are valid :class:`~baseaicore.ProviderKind` members but
have no adapter wired here yet; naming one is a configuration error today, not a silent fallback
to Ollama.
"""

# E6: ModelRack's `DEFAULT_MODEL` declares an 8.5 GB model, so an unscripted `FakeProvider()`
# trips the `insufficient_vram` hard constraint (routing.md §4) whenever the host GPU is busy —
# found at E4, 2026-09-04. The fix lives here, not in ModelRack: `DEFAULT_MODEL` is a published
# contract three applications' fakes read, and changing it would need a modelrack release to fix
# one application's routing demonstration. This module already owns the fake's construction
# (`FakeProvider()` below), so it declares a small model instead — one whose weights, layers,
# kv_heads and head_dim could describe a real tiny model, not just a shrunk number.
#
# VRAM arithmetic (`constraints.estimate_vram`; `LOADING_OVERHEAD_FACTOR=1.05`,
# `ACTIVATION_OVERHEAD_BYTES=256 MiB` fixed, f16 KV assumed when a profile leaves precision
# unconfigured — `constraints.py`), at the worst case this model ever serves: `served_context`
# defaults to this model's own `max_context` (32 768, unchanged from `DEFAULT_MODEL`, so every
# shipped task profile's `min_context_tokens` up to and including `tools.agent.local_large`'s
# 32 768 is still satisfied) whenever the caller's runtime profile does not configure a context:
#
#   weights = 47_000_000 * 1.05                            =  49_350_000 B
#   kv      = (2 * 4 * 2 * 64 * 2) * 32_768                 =  67_108_864 B
#   activation                                              = 268_435_456 B   (fixed)
#   total                                                  ~= 384_894_320 B   (~367 MiB)
#
# 384_894_320 + DEFAULT_VRAM_HEADROOM_BYTES (512 MiB) ~= 921_765_232 B, comfortably under even a
# machine reporting ~1 GiB free — so this fake never trips `insufficient_vram` on its own.
_FAKE_MODEL_SIZE_BYTES: Final = 47_000_000
_FAKE_MODEL_PARAMETER_COUNT: Final = 45_000_000
_FAKE_MODEL_EMBEDDING_DIM: Final = 256
_FAKE_MODEL_LAYERS: Final = 4
_FAKE_MODEL_ATTENTION_HEADS: Final = 4
_FAKE_MODEL_KV_HEADS: Final = 2
_FAKE_MODEL_HEAD_DIM: Final = 64
_FAKE_MODEL_VOCAB_SIZE: Final = 32_000


def _fake_model(overrides: FakeProviderSettings) -> FakeModel:
    """Build the catalogue entry :func:`build_provider` hands the fake provider (E6).

    Args:
        overrides: ``settings.provider.fake``. Either all four fields are set, provoking
            ``insufficient_vram`` on purpose, or none are, keeping the small built-in default.

    Returns:
        A :class:`~modelrack.testing.FakeModel` built from :data:`~modelrack.testing.
        DEFAULT_MODEL` by :func:`dataclasses.replace`.

    Raises:
        ConfigurationError: exactly one, two or three of ``overrides``' four fields are set. The
            KV term dominates the VRAM estimate at any interesting context length, so a partial
            override cannot reliably provoke the rejection this block exists to reach.
    """
    from modelrack.testing import DEFAULT_MODEL

    model = dataclasses.replace(
        DEFAULT_MODEL,
        name="fake-model:tiny-q8_0",
        parameter_count=_FAKE_MODEL_PARAMETER_COUNT,
        active_parameter_count=_FAKE_MODEL_PARAMETER_COUNT,
        size_bytes=_FAKE_MODEL_SIZE_BYTES,
        embedding_dim=_FAKE_MODEL_EMBEDDING_DIM,
        layers=_FAKE_MODEL_LAYERS,
        attention_heads=_FAKE_MODEL_ATTENTION_HEADS,
        kv_heads=_FAKE_MODEL_KV_HEADS,
        head_dim=_FAKE_MODEL_HEAD_DIM,
        vocab_size=_FAKE_MODEL_VOCAB_SIZE,
    )
    match (overrides.size_bytes, overrides.layers, overrides.kv_heads, overrides.head_dim):
        case (None, None, None, None):
            return model
        case (int() as size_bytes, int() as layers, int() as kv_heads, int() as head_dim):
            return dataclasses.replace(
                model, size_bytes=size_bytes, layers=layers, kv_heads=kv_heads, head_dim=head_dim
            )
        case _:
            names = ("size_bytes", "layers", "kv_heads", "head_dim")
            values = (
                overrides.size_bytes,
                overrides.layers,
                overrides.kv_heads,
                overrides.head_dim,
            )
            missing = [name for name, value in zip(names, values, strict=True) if value is None]
            raise ConfigurationError(
                "provider.fake requires size_bytes, layers, kv_heads and head_dim together: the "
                "VRAM estimate's KV term dominates size_bytes at any interesting context length, "
                "so a partial override cannot reliably provoke insufficient_vram; "
                f"missing {missing}.",
                details={"field": "provider.fake", "missing": missing},
            )


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    """One registered provider: its operator-chosen name, its kind, its egress class, its handle.

    LoadCoach 1.1 registers providers by name and kind into one tagged registry
    (ADR-0055). The name is what explanations, the models UI and ``doctor`` refer to; for a
    singular ``[provider]`` block it is the kind (ADR-0077 rule 1).

    Attributes:
        name: The registration's name, unique within one configuration.
        kind: The provider kind this registration was built from.
        is_remote: The registration's **declared** ``remote`` flag. Never inferred from the kind
            or the URL (ADR-0055 rule 4).
        provider: The constructed handle. Opens no connection by itself.
        adapters_registered: Whether this registration was handed any adapters — ``True`` when
            the operator's directory held at least one available adapter and this provider can
            hot-swap, ``False`` when it can hot-swap and was handed none, and ``None`` for a
            provider that has no concept of adapters (ADR-0074). Recorded from what LoadCoach
            offered, never read back from ``list_adapters()``, which moves during a pending
            restart.
    """

    name: str
    kind: str
    is_remote: bool
    provider: Provider
    adapters_registered: bool | None = None


def build_registrations(settings: Settings) -> tuple[ProviderRegistration, ...]:
    """Construct every provider this configuration registers, in a stable order.

    The one composition root for providers, plural. A singular ``[provider]`` block yields exactly
    one registration named after its kind, declaring ``remote = false``; named
    ``[providers.<name>]`` blocks yield one each, in name order so that discovery, ``doctor`` and
    every explanation list them the same way twice running. A configuration writing both forms
    never reaches here — :func:`~loadcoach.config.load_settings` refuses it (ADR-0077 rule 3).

    Args:
        settings: The resolved application configuration.

    Where ``[adapters] directory`` is configured, every registration whose provider declares
    ``adapter_hot_swap`` is handed the adapters that directory holds (ADR-0061 rule 3). Empty
    directory, empty configuration or a provider that cannot hot-swap: nothing is offered, and
    nothing about the registration changes.

    Returns:
        One :class:`ProviderRegistration` per configured provider. Never empty: with no
        configuration at all, the singular block's defaults are one Ollama registration, which is
        LoadCoach 1.0's behaviour unchanged.

    Raises:
        ConfigurationError: A registration names an unsupported kind, or a ``fake`` registration
            sets only some of its four model-shape overrides.
    """
    named = settings.providers.registrations
    if named:
        built = tuple(
            ProviderRegistration(
                name=name,
                kind=registration.kind,
                is_remote=registration.remote,
                provider=_build_one(registration, field=f"providers.{name}.kind", name=name),
            )
            for name, registration in sorted(named.items())
        )
    else:
        singular = settings.provider
        built = (
            ProviderRegistration(
                name=singular.kind,
                kind=singular.kind,
                is_remote=False,
                provider=_build_one(
                    singular.as_registration(), field="provider.kind", name=singular.kind
                ),
            ),
        )
    return _offer_adapters(built, settings)


def _offer_adapters(
    registrations: tuple[ProviderRegistration, ...], settings: Settings
) -> tuple[ProviderRegistration, ...]:
    """Hand every hot-swapping provider the adapters the operator's directory holds.

    The conversion from a reviewed ``model.adapter_manifest`` into ModelRack's
    :class:`~modelrack.adapters.AdapterRegistration` happens **here, in the application**:
    ModelRack never reads the directory (ADR-0061 rule 3), and this is the one place that gap is
    bridged. A provider that declares no ``adapter_hot_swap`` is skipped rather than asked and
    refused — a remote endpoint is not a misconfiguration to report, it is simply not a provider
    adapters can reach, which is what makes ADR-0065's local-only invariant hold by construction.

    Unavailable entries never reach a provider: an adapter whose artifact no longer matches its
    manifest is refused at the directory, before anything could serve it.

    Returns:
        The registrations, each carrying what it was actually handed as
        :attr:`ProviderRegistration.adapters_registered` — the fact LoadCoach must state on every
        runtime profile it builds for such a provider (ADR-0074), stated here because this is the
        only place that knows it.
    """
    from loadcoach.infrastructure.adapters import read_directory, registrations_from

    directory = settings.adapters.path
    adapters = () if directory is None else registrations_from(read_directory(directory).available)
    offered: list[ProviderRegistration] = []
    for registration in registrations:
        if not registration.provider.capabilities().adapter_hot_swap:
            offered.append(registration)
            continue
        if adapters:
            registration.provider.register_adapters(adapters)
        offered.append(dataclasses.replace(registration, adapters_registered=bool(adapters)))
    return tuple(offered)


def _build_one(settings: ProviderRegistrationSettings, *, field: str, name: str = "") -> Provider:
    """Construct one registration's provider, naming ``field`` in any refusal."""
    if settings.kind == "llamacpp":
        return _build_llamacpp(settings, field=field, name=name)
    if settings.kind == "ollama":
        from modelrack.providers.ollama import OllamaProvider

        return OllamaProvider(settings.base_url, timeout=settings.timeout_seconds)
    if settings.kind == "fake":
        from modelrack.testing import FakeProvider, FakeScript

        return FakeProvider(FakeScript(models=(_fake_model(settings.fake),)))
    raise ConfigurationError(
        f"{field}={settings.kind!r} is not supported; expected one of "
        f"{sorted(SUPPORTED_PROVIDER_KINDS)!r}.",
        details={"field": field, "value": settings.kind},
    )


def _build_llamacpp(settings: ProviderRegistrationSettings, *, field: str, name: str) -> Provider:
    """Construct a supervised llama.cpp server for one registration (ADR-0062).

    Args:
        settings: That registration's block.
        field: The configuration key to name in a refusal.
        name: The registration's name, which the default state directory is scoped by so two
            registrations never share a supervisor's state.

    Returns:
        The provider. Launches nothing here: a server starts when a model is first loaded.

    Raises:
        ConfigurationError: ``model_directory`` is empty. There is no default worth guessing —
            a wrong directory is a server serving weights nobody asked for — and an empty one is
            the case an operator hits by copying a block, so it is named rather than defaulted.
    """
    from modelrack.providers.llamacpp import LlamaCppProvider

    directory = settings.model_directory.strip()
    if not directory:
        key = field.removesuffix(".kind")
        raise ConfigurationError(
            f"{key}.model_directory is required for kind='llamacpp': the server is launched over "
            "a directory of GGUF weights, and there is no default worth guessing.",
            details={"field": f"{key}.model_directory"},
        )
    state = settings.state_dir.strip()
    if state:
        state_path = Path(state).expanduser()
    else:
        from loadcoach.config import data_dir

        state_path = data_dir() / "llamacpp" / (name or settings.kind)
    state_path.mkdir(parents=True, exist_ok=True)
    return LlamaCppProvider(
        Path(directory).expanduser(),
        state_dir=state_path,
        server_path=settings.server_path,
        timeout=settings.timeout_seconds,
    )


def build_provider(settings: ProviderSettings) -> Provider:
    """Construct the configured :class:`~modelrack.provider.Provider`.

    Args:
        settings: ``settings.provider`` from the resolved application configuration.

    Returns:
        A provider satisfying the :class:`~modelrack.provider.Provider` protocol. Opens no
        connection by itself.

    Raises:
        ConfigurationError: ``settings.kind`` is not one of :data:`SUPPORTED_PROVIDER_KINDS`, or
            ``settings.fake`` sets only some of ``size_bytes``/``layers``/``kv_heads``/``head_dim``
            (E6: the KV term dominates the estimate, so a partial override cannot reliably provoke
            ``insufficient_vram`` — see :class:`~loadcoach.config.FakeProviderSettings`).
    """
    return _build_one(settings.as_registration(), field="provider.kind")
