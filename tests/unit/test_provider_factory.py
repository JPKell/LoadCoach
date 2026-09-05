"""``build_provider``'s fake branch (E6) and ``build_registrations``' naming rules (LC-E1).

Fast, no database: exercises :func:`loadcoach.infrastructure.providers.factory._fake_model`
directly and through :func:`build_provider`. The end-to-end VRAM-constraint behaviour these numbers
exist to satisfy is covered by ``tests/integration/test_fake_provider_vram.py``.
"""

from __future__ import annotations

import pytest
from baseaicore import ConfigurationError
from modelrack.testing import DEFAULT_MODEL, FakeProvider

from loadcoach.config import FakeProviderSettings, ProviderSettings, Settings
from loadcoach.infrastructure.providers.factory import build_provider, build_registrations


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


# --------------------------------------------------------------------------------------------
# LC-E1 (ADR-0055, ADR-0077) — registrations by name and kind
# --------------------------------------------------------------------------------------------


def test_a_singular_block_is_one_registration_named_after_its_kind() -> None:
    """ADR-0077 rule 1: the 1.0 configuration shape keeps working, and it acquires a name.

    The name is the kind rather than "default", because it is what explanations, the models UI and
    ``doctor`` print, and `ollama` tells an operator something that `default` does not.
    """
    registrations = build_registrations(Settings())

    assert len(registrations) == 1
    (registration,) = registrations
    assert registration.name == "ollama"
    assert registration.kind == "ollama"
    assert registration.is_remote is False


def test_a_singular_fake_block_names_itself_fake_and_is_local() -> None:
    settings = Settings.model_validate({"provider": {"kind": "fake"}})

    (registration,) = build_registrations(settings)

    assert (registration.name, registration.kind, registration.is_remote) == ("fake", "fake", False)
    assert isinstance(registration.provider, FakeProvider)


def test_named_blocks_register_in_name_order_with_their_declared_egress_class() -> None:
    """One pool, tagged (ADR-0055 rule 3), and `remote` is declared rather than inferred.

    Name order, not file order: discovery, ``doctor`` and every explanation must list them the
    same way twice running.
    """
    settings = Settings.model_validate(
        {
            "providers": {
                "zeta": {"kind": "fake"},
                "alpha": {"kind": "ollama", "base_url": "http://127.0.0.1:11434"},
                "hosted": {"kind": "ollama", "base_url": "http://elsewhere", "remote": True},
            }
        }
    )

    registrations = build_registrations(settings)

    assert [r.name for r in registrations] == ["alpha", "hosted", "zeta"]
    assert [r.is_remote for r in registrations] == [False, True, False]


def test_a_remote_flag_is_not_inferred_from_a_kind_or_a_url() -> None:
    """ADR-0055 rule 4: an endpoint on loopback is local and the same kind hosted is remote."""
    settings = Settings.model_validate(
        {
            "providers": {
                "loopback": {"kind": "ollama", "base_url": "https://api.example.com"},
            }
        }
    )

    (registration,) = build_registrations(settings)

    # A hosted-looking URL with no declaration is local, because nothing infers: that is the
    # footgun ADR-0055 names, and the compensating control is elsewhere.
    assert registration.is_remote is False


def test_an_unsupported_kind_in_a_named_block_names_that_block() -> None:
    settings = Settings.model_validate({"providers": {"gpu": {"kind": "vllm"}}})

    with pytest.raises(ConfigurationError) as raised:
        build_registrations(settings)

    assert raised.value.details["field"] == "providers.gpu.kind"


def test_a_named_fake_block_carries_its_own_model_overrides() -> None:
    """`[providers.<name>.fake]` is the singular block's `[provider.fake]`, per registration."""
    settings = Settings.model_validate(
        {
            "providers": {
                "sim": {
                    "kind": "fake",
                    "fake": {
                        "size_bytes": DEFAULT_MODEL.size_bytes,
                        "layers": DEFAULT_MODEL.layers,
                        "kv_heads": DEFAULT_MODEL.kv_heads,
                        "head_dim": DEFAULT_MODEL.head_dim,
                    },
                }
            }
        }
    )

    (registration,) = build_registrations(settings)

    assert isinstance(registration.provider, FakeProvider)
    (model,) = registration.provider.script.models
    assert model.size_bytes == DEFAULT_MODEL.size_bytes
