"""Two operator-facing gaps H3's live journey found: a leaked server, and unwritable policy.

Neither is about adapters, and both were invisible from inside a test suite that never ran a
server as a process:

* **A provider handle was never released at shutdown.** A supervising provider owns an operating
  system process, and dropping the handle does not end it. So a stopped `loadcoach serve` left its
  `llama-server` running and holding the whole card, and the *next* candidate was then refused
  `insufficient_vram` before its classification was ever considered — a defect in shutdown
  presenting as a defect in routing.
* **A deployment's task profiles were not expressible.** `bootstrap` imported the file shipped
  inside the installed package, and there was no key naming another, so changing the policy a
  deployment routes under meant editing an installed package or a database row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from baseaicore import ConfigurationError

from loadcoach.config import Settings
from loadcoach.infrastructure.providers.factory import ProviderRegistration, close_registrations
from loadcoach.services.task_profiles import (
    DEFAULT_TASK_PROFILES_PATH,
    read_task_profiles_file,
    task_profiles_path_for,
)

if TYPE_CHECKING:
    from pathlib import Path


class _Closable:
    """A provider handle that records whether it was released."""

    def __init__(self, *, raises: bool = False) -> None:
        self.closed = 0
        self._raises = raises

    def close(self) -> None:
        self.closed += 1
        if self._raises:
            message = "this handle could not be released"
            raise RuntimeError(message)


def _registration(name: str, provider: Any) -> ProviderRegistration:
    return ProviderRegistration(name=name, kind="llamacpp", is_remote=False, provider=provider)


def test_every_provider_is_closed_at_shutdown() -> None:
    """A supervised server outlives its handle; only `close()` ends it."""
    first, second = _Closable(), _Closable()

    close_registrations([_registration("local", first), _registration("second", second)])

    assert (first.closed, second.closed) == (1, 1)


def test_one_handle_that_cannot_be_released_does_not_leak_the_rest() -> None:
    """A failed release is a reason to log and continue, never a reason to leak the others."""
    failing, healthy = _Closable(raises=True), _Closable()

    close_registrations([_registration("broken", failing), _registration("local", healthy)])

    assert healthy.closed == 1


def test_a_provider_with_no_close_is_skipped_rather_than_crashing_shutdown() -> None:
    """Not every provider owns a process; one that owns none has nothing to release."""

    class _Bare:
        pass

    close_registrations([_registration("bare", _Bare())])


def test_no_configured_path_means_the_shipped_profiles() -> None:
    assert task_profiles_path_for(Settings().routing) == DEFAULT_TASK_PROFILES_PATH


def test_a_configured_path_is_the_one_imported(tmp_path: Path) -> None:
    """A deployment can write its own routing policy without editing an installed package."""
    profiles = tmp_path / "task_profiles.toml"
    profiles.write_text(
        """
[task_profiles."site.local"]
version = "1.0.0"
description = "This deployment's own profile."
[task_profiles."site.local".weights]
instruction_following = 1.0
[task_profiles."site.local".constraints]
min_context_tokens = 2048
allow_remote_providers = true
[task_profiles."site.local".execution]
temperature = 0.0
max_output_tokens = 32
response_format = "text"
max_attempts = 1
fallback_depth = 0
""",
        encoding="utf-8",
    )
    settings = Settings.model_validate({"routing": {"task_profiles_path": str(profiles)}})

    resolved = task_profiles_path_for(settings.routing)
    loaded = read_task_profiles_file(resolved)

    assert resolved == profiles
    assert [profile.profile_id for profile in loaded] == ["site.local"]
    assert loaded[0].constraints.allow_remote_providers is True


def test_a_path_that_is_not_a_file_is_refused_rather_than_falling_back(tmp_path: Path) -> None:
    """Routing under profiles you did not write is not something you could tell from outside."""
    settings = Settings.model_validate(
        {"routing": {"task_profiles_path": str(tmp_path / "absent.toml")}}
    )

    with pytest.raises(ConfigurationError) as caught:
        task_profiles_path_for(settings.routing)

    assert "task_profiles_path" in caught.value.message
