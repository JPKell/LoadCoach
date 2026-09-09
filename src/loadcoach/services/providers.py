"""loadcoach.services.providers — reading and editing ``[providers.<name>]`` in the config file.

[ADR-0117](../../docs/adr/0117-provider-registrations-are-edited-in-place-in-the-config-file.md):
``config.toml`` stays the single source of truth for a provider registration, and the web admin
edits it *in place*, so an operator's comments, key order and formatting survive a write. Nothing
here writes any other part of the file, and ``providers.allow_remote`` — the egress boundary — is
refused by name like every other config-only key.

The write is validated before it lands: the candidate document is loaded through the
application's own :func:`~loadcoach.config.load_settings`, so a document this module would write
is a document the next start would accept. A file that changed under the editor is refused rather
than overwritten, which is what ``base_digest`` is for.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

import tomlkit
from baseaicore import SuiteError, ValidationError
from pydantic import ValidationError as PydanticValidationError

from loadcoach.config import ENV_PREFIX, ProviderRegistrationSettings, load_settings

if TYPE_CHECKING:
    from tomlkit import TOMLDocument

    from loadcoach.config import Settings

__all__ = [
    "WRITABLE_FIELDS",
    "ProviderConfigChanged",
    "RegistrationView",
    "config_digest",
    "delete_registration",
    "describe_registrations",
    "save_registration",
]

WRITABLE_FIELDS: Final[tuple[str, ...]] = (
    "kind",
    "base_url",
    "timeout_seconds",
    "remote",
    "model_directory",
    "state_dir",
    "server_path",
)
"""The keys of a registration this module will write.

Every field of :class:`~loadcoach.config.ProviderRegistrationSettings` except ``fake``, which
scripts a test double's declared model and belongs to a developer editing the file, not to an
operator editing a provider.
"""


class ProviderConfigChanged(SuiteError):
    """The file changed since it was read; the write is refused rather than applied blind."""

    code: ClassVar[str] = "CONFLICT"


@dataclass(frozen=True, slots=True)
class RegistrationView:
    """One registration as the page and ``GET /providers`` render it."""

    name: str
    kind: str
    base_url: str
    timeout_seconds: float
    remote: bool
    model_directory: str
    state_dir: str
    server_path: str
    shadowed_by: str

    def as_json(self) -> dict[str, Any]:
        """This registration as JSON, with ``shadowed_by`` empty when nothing shadows it."""
        return {
            "name": self.name,
            "kind": self.kind,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
            "remote": self.remote,
            "model_directory": self.model_directory,
            "state_dir": self.state_dir,
            "server_path": self.server_path,
            "shadowed_by": self.shadowed_by,
        }


def _shadowing_variables() -> dict[str, str]:
    """``registration name -> variable`` for every ``LOADCOACH_PROVIDERS__*`` in the environment.

    The environment sits above the file (configuration standards §7), so a variable pinning a
    registration's field makes a file edit inert until it is unset. The page says so rather than
    letting an operator watch a saved value do nothing.
    """
    prefix = f"{ENV_PREFIX}PROVIDERS__"
    found: dict[str, str] = {}
    for key in os.environ:
        if not key.startswith(prefix):
            continue
        name = key[len(prefix) :].split("__", 1)[0].lower()
        found.setdefault(name, key)
    return found


def describe_registrations(settings: Settings) -> list[RegistrationView]:
    """Every configured registration, in name order, with what shadows it.

    Both configured forms are described the same way: a singular ``[provider]`` block is the one
    registration it is (ADR-0077 rule 1), named after its kind.

    Args:
        settings: The loaded configuration.

    Returns:
        One :class:`RegistrationView` per registration, sorted by name.
    """
    shadowing = _shadowing_variables()
    named = settings.providers.registrations or {
        settings.provider.name: settings.provider.as_registration()
    }
    return [
        RegistrationView(
            name=name,
            kind=registration.kind,
            base_url=registration.base_url,
            timeout_seconds=registration.timeout_seconds,
            remote=registration.remote,
            model_directory=registration.model_directory,
            state_dir=registration.state_dir,
            server_path=registration.server_path,
            shadowed_by=shadowing.get(name, ""),
        )
        for name, registration in sorted(named.items())
    ]


def config_digest(config_path: Path) -> str:
    """The digest of the file as it stands, or ``""`` when there is no file yet.

    The page carries this into the form so that :func:`save_registration` can refuse a write
    whose base document is no longer what was read (ADR-0117 consequence 4).
    """
    if not config_path.exists():
        return ""
    return hashlib.sha256(config_path.read_bytes()).hexdigest()


def _read_document(config_path: Path) -> TOMLDocument:
    """Parse the file with comments and formatting intact; an empty document if there is none."""
    if not config_path.exists():
        return tomlkit.document()
    return tomlkit.parse(config_path.read_text(encoding="utf-8"))


def _providers_table(document: TOMLDocument) -> Any:
    """The ``[providers]`` table, created if absent, with the singular block folded into it.

    ADR-0077 rule 3 refuses a file that writes both forms, so a file using the singular
    ``[provider]`` block has that block *moved* — the item itself, with its comments — under
    ``[providers]`` as the registration it already was, named after its kind.
    """
    if "providers" not in document:
        document["providers"] = tomlkit.table()
    providers = document["providers"]
    if "provider" in document:
        singular = document["provider"]
        name = str(singular.get("kind", "ollama"))
        document.pop("provider")
        singular.pop("fake", None)
        providers[name] = singular
    return providers


def _validated(config_path: Path, document: TOMLDocument) -> None:
    """Write ``document`` beside the file, load it, and leave the candidate in place on success.

    Args:
        config_path: The file being edited.
        document: The candidate document.

    Raises:
        SuiteError: Whatever :func:`~loadcoach.config.load_settings` raises for a document it
            refuses — reported with the file untouched.
    """
    candidate = config_path.with_name(config_path.name + ".new")
    candidate.write_text(tomlkit.dumps(document), encoding="utf-8")
    try:
        load_settings(config_path=candidate)
    except Exception:
        candidate.unlink(missing_ok=True)
        raise
    if config_path.exists():
        config_path.replace(config_path.with_name(config_path.name + ".bak"))
    candidate.replace(config_path)


def _write(config_path: Path, base_digest: str | None, edit: Any) -> None:
    """Apply ``edit`` to the parsed document and land it, refusing a stale base.

    Args:
        config_path: The file to edit; its parent must exist.
        base_digest: The digest the caller read, or ``None`` to skip the check.
        edit: Called with the parsed document; mutates it in place.

    Raises:
        ProviderConfigChanged: The file changed since ``base_digest`` was taken.
    """
    if base_digest is not None and base_digest != config_digest(config_path):
        message = (
            f"{config_path} changed since this page was loaded; the write was refused so that an "
            "edit made elsewhere is not overwritten. Reload the page and apply the change again."
        )
        raise ProviderConfigChanged(message, details={"file": str(config_path)})
    document = _read_document(config_path)
    edit(document)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    _validated(config_path, document)


def save_registration(
    config_path: Path,
    name: str,
    values: dict[str, Any],
    *,
    base_digest: str | None = None,
) -> None:
    """Create or change ``[providers.<name>]``, leaving the rest of the file exactly as it was.

    Args:
        config_path: The configuration file to edit.
        name: The registration's name — the operator's, and what explanations refer to.
        values: Any of :data:`WRITABLE_FIELDS`. Absent keys keep the registration's current
            value; an empty string for an optional key removes it.
        base_digest: The digest the form was rendered from, if the caller took one.

    Raises:
        ValidationError: ``name`` is not a usable table key, or ``values`` names a key this
            module refuses to write — ``fake``, ``allow_remote``, or anything outside the
            registration.
        ProviderConfigChanged: The file changed since ``base_digest`` was taken.
        SuiteError: The resulting document is not a configuration this application would load.
    """
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        message = (
            f"{name!r} is not a usable registration name: letters, digits, hyphen and underscore, "
            "and not empty."
        )
        raise ValidationError(message, details={"field": "name", "value": name})
    refused = sorted(set(values) - set(WRITABLE_FIELDS))
    if refused:
        message = (
            f"{', '.join(refused)} cannot be written here. A registration's writable keys are "
            f"{', '.join(WRITABLE_FIELDS)}; `allow_remote` is the egress boundary and stays "
            "config-only (ADR-0117 decision 3)."
        )
        raise ValidationError(message, details={"refused": refused})
    # Validated here as one registration before it reaches the document, so a bad field is
    # reported as itself rather than as a failure to load the whole file — and reported in this
    # application's error envelope rather than as pydantic's own exception type.
    try:
        ProviderRegistrationSettings.model_validate({"kind": "ollama", **values})
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "provider"
        message = f"{field}: {first['msg']}"
        raise ValidationError(message, details={"field": field, "name": name}) from exc

    def _edit(document: TOMLDocument) -> None:
        providers = _providers_table(document)
        table = providers.get(name)
        if table is None:
            table = tomlkit.table()
            providers[name] = table
        for field_name in WRITABLE_FIELDS:
            if field_name not in values:
                continue
            value = values[field_name]
            if value == "" and field_name != "kind":
                table.pop(field_name, None)
                continue
            table[field_name] = value

    _write(config_path, base_digest, _edit)


def delete_registration(config_path: Path, name: str, *, base_digest: str | None = None) -> None:
    """Remove ``[providers.<name>]``, leaving the rest of the file exactly as it was.

    Args:
        config_path: The configuration file to edit.
        name: The registration to remove.
        base_digest: The digest the form was rendered from, if the caller took one.

    Raises:
        ValidationError: No registration of that name is configured, or it is the last one — an
            application with no provider registers nothing and serves nothing, and deleting the
            last one from a browser is a failure mode with no path back through the browser.
        ProviderConfigChanged: The file changed since ``base_digest`` was taken.
        SuiteError: The resulting document is not a configuration this application would load.
    """

    def _edit(document: TOMLDocument) -> None:
        providers = _providers_table(document)
        if name not in providers:
            message = f"no provider registration named {name!r} is configured here."
            raise ValidationError(message, details={"field": "name", "value": name})
        remaining = [key for key in providers if key != name and isinstance(providers[key], dict)]
        if not remaining:
            message = (
                f"{name!r} is the only provider registration configured; removing it would leave "
                "this application with no provider at all. Add another one first."
            )
            raise ValidationError(message, details={"field": "name", "value": name})
        providers.pop(name)

    _write(config_path, base_digest, _edit)
