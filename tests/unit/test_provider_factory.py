"""``build_provider``'s fake branch (E6): the declared model, and its ``[provider.fake]`` override.

Fast, no database: exercises :func:`loadcoach.infrastructure.providers.factory._fake_model`
directly and through :func:`build_provider`. The end-to-end VRAM-constraint behaviour these numbers
exist to satisfy is covered by ``tests/integration/test_fake_provider_vram.py``.
"""

from __future__ import annotations

import pytest
from baseaicore import ConfigurationError
from modelrack.testing import DEFAULT_MODEL, FakeProvider

from loadcoach.config import FakeProviderSettings, ProviderSettings
from loadcoach.infrastructure.providers.factory import build_provider


def test_the_default_fake_model_is_small_and_not_named_8b() -> None:
    """Requirement 1: the shipped default no longer claims to be the 8.5 GB `DEFAULT_MODEL`."""
    provider = build_provider(ProviderSettings(kind="fake"))
    assert isinstance(provider, FakeProvider)
    (model,) = provider.script.models
    assert model.size_bytes is not None
    assert model.size_bytes < DEFAULT_MODEL.size_bytes
    assert "8b" not in model.name


def test_the_default_fake_model_keeps_default_models_context_ceiling() -> None:
    """``max_context`` stays 32 768 so every shipped local task profile's ``min_context_tokens``
    (up to `tools.agent.local_large`'s 32 768) is still satisfiable when unconfigured."""
    provider = build_provider(ProviderSettings(kind="fake"))
    assert isinstance(provider, FakeProvider)
    (model,) = provider.script.models
    assert model.max_context == DEFAULT_MODEL.max_context == 32_768


def test_fake_settings_override_reproduces_the_original_default_models_geometry() -> None:
    """Requirement 2: an operator can dial the fake back up to the pre-E6 declared numbers."""
    settings = ProviderSettings(
        kind="fake",
        fake=FakeProviderSettings(size_bytes=8_540_000_000, layers=32, kv_heads=8, head_dim=128),
    )
    provider = build_provider(settings)
    assert isinstance(provider, FakeProvider)
    (model,) = provider.script.models
    assert model.size_bytes == 8_540_000_000
    assert model.layers == 32
    assert model.kv_heads == 8
    assert model.head_dim == 128


@pytest.mark.parametrize(
    "overrides",
    [
        {"size_bytes": 8_540_000_000},
        {"layers": 32},
        {"size_bytes": 8_540_000_000, "layers": 32},
        {"size_bytes": 8_540_000_000, "layers": 32, "kv_heads": 8},
    ],
)
def test_a_partial_fake_override_is_refused(overrides: dict[str, int]) -> None:
    """A lone ``size_bytes`` cannot reliably provoke ``insufficient_vram`` (§0.2's KV term), so a
    partial override is a configuration error rather than a silently-incoherent model."""
    settings = ProviderSettings(kind="fake", fake=FakeProviderSettings(**overrides))
    with pytest.raises(ConfigurationError) as caught:
        build_provider(settings)
    assert caught.value.details["field"] == "provider.fake"
    for name in ("size_bytes", "layers", "kv_heads", "head_dim"):
        if name not in overrides:
            assert name in caught.value.details["missing"]
