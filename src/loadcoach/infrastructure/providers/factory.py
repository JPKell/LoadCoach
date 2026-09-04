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
from typing import TYPE_CHECKING, Final

from baseaicore import ConfigurationError

if TYPE_CHECKING:
    from modelrack.provider import Provider
    from modelrack.testing import FakeModel

    from loadcoach.config import FakeProviderSettings, ProviderSettings

__all__ = ["SUPPORTED_PROVIDER_KINDS", "build_provider"]

SUPPORTED_PROVIDER_KINDS: frozenset[str] = frozenset({"ollama", "fake"})
"""``provider.kind`` values this phase can construct.

``"ollama"`` is the production adapter (spec §5: "a model provider (Ollama by default)").
``"fake"`` constructs :class:`~modelrack.testing.FakeProvider` so the running application — not
just its unit tests — can be exercised with no GPU, no Ollama and no network.
``openai_compatible``, ``llamacpp`` and ``vllm`` are valid :class:`~baseaicore.ProviderKind`
members but have no adapter wired here yet; naming one is a configuration error today, not a
silent fallback to Ollama.
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
    if settings.kind == "ollama":
        from modelrack.providers.ollama import OllamaProvider

        return OllamaProvider(settings.base_url, timeout=settings.timeout_seconds)
    if settings.kind == "fake":
        from modelrack.testing import FakeProvider, FakeScript

        return FakeProvider(FakeScript(models=(_fake_model(settings.fake),)))
    raise ConfigurationError(
        f"provider.kind={settings.kind!r} is not supported; expected one of "
        f"{sorted(SUPPORTED_PROVIDER_KINDS)!r}.",
        details={"field": "provider.kind", "value": settings.kind},
    )
