"""The adapter directory: read reviewed manifests, draft new ones, refuse the rest.

ADR-0061: the registry is an operator-owned directory of artifacts plus one reviewed
``model.adapter_manifest`` per adapter. Identity is the artifact hash and the path is a locator,
so a rename is transparent and a content change is a different adapter — and a manifest whose
recorded hash no longer matches its artifact makes that adapter **unavailable**, named, until a
rescan (rule 5). Fail closed, never a silent misattribution.

The file layout this module reads and writes::

    <directory>/factcheck.gguf                  the served artifact
    <directory>/factcheck.manifest.json         the reviewed manifest, a SetSpec envelope
    <directory>/factcheck.manifest.draft.json   what `adapters scan` writes for review

A draft is never read as a registration. "The scan drafts, a human keeps" is rule 4, and it is
enforced by the suffix rather than by a flag inside the document: a file an operator has not
renamed cannot be mistaken for one they reviewed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from baseaicore import DataClassification, IdentityConfidence, normalize_digest
from pydantic import ValidationError as PydanticValidationError
from setspec import GeneratorInfo, SchemaVersion, dump_envelope, load_envelope
from setspec.model.v1 import AdapterManifestIn

from loadcoach.__about__ import __version__

if TYPE_CHECKING:
    from collections.abc import Sequence

    from modelrack.adapters import AdapterRegistration

__all__ = [
    "DRAFT_SUFFIX",
    "MANIFEST_SCHEMA",
    "MANIFEST_SUFFIX",
    "MANIFEST_VERSION",
    "AdapterEntry",
    "DirectoryReading",
    "DraftOutcome",
    "draft_manifests",
    "read_directory",
    "registrations_from",
]

MANIFEST_SCHEMA: Final = "model.adapter_manifest"
MANIFEST_VERSION: Final = SchemaVersion(1, 0)
MANIFEST_SUFFIX: Final = ".manifest.json"
DRAFT_SUFFIX: Final = ".manifest.draft.json"
ARTIFACT_SUFFIX: Final = ".gguf"

_HASH_CHUNK_BYTES: Final = 1024 * 1024
"""Read size for hashing an artifact. A LoRA GGUF is tens of megabytes, not gigabytes, but
streaming it costs nothing and keeps a large one from being read into memory whole."""


def sha256_of(path: Path) -> str:
    """Return the normalized ``sha256:`` digest of a file's contents.

    Args:
        path: The file to hash.

    Returns:
        ``"sha256:"`` followed by 64 lowercase hex characters, the form ADR-0024 §2 fixes and the
        form every digest in the suite is compared in.

    Raises:
        OSError: The file could not be read. The caller turns that into an unavailable adapter
            with a reason rather than a crash.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    normalized = normalize_digest(digest.hexdigest())
    # `normalize_digest` returns None only for input it cannot read as a digest; 64 hex
    # characters from hashlib is never that.
    assert normalized is not None  # noqa: S101 — a hashlib digest always normalizes
    return normalized


@dataclass(frozen=True, slots=True)
class AdapterEntry:
    """One adapter the directory describes, and whether it can be used.

    Attributes:
        name: The manifest's name — the pin and display name.
        manifest_path: The reviewed manifest this entry was read from.
        artifact_path: Where the served artifact is, resolved against the directory.
        artifact_sha256: The digest the manifest records. **The identity.**
        source_sha256: The training checkpoint's digest, for lineage only.
        base_model_name: The base this adapter declares.
        base_artifact_digest: That base's digest, when the manifest's author proved one.
        base_confidence: ``DIGEST`` when the base carries one, ``NAME_ONLY`` otherwise — the
            existing machinery, not a parallel flag (ADR-0058 §5).
        declared_capabilities: The vocabulary terms the manifest claims, already validated.
        data_classification: Required by the payload, so always present here (ADR-0065 rule 1).
        notes: The reviewer's free text.
        payload: The manifest payload exactly as it was read.
        available: Whether this adapter may be registered at all.
        unavailable_reason: Why not. ``None`` when :attr:`available`.
    """

    name: str
    manifest_path: Path
    artifact_path: Path
    artifact_sha256: str
    source_sha256: str | None
    base_model_name: str
    base_artifact_digest: str | None
    base_confidence: IdentityConfidence
    declared_capabilities: tuple[str, ...]
    data_classification: DataClassification
    notes: str | None
    payload: dict[str, Any]
    available: bool
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DirectoryReading:
    """Everything one pass over the directory found.

    Attributes:
        directory: The directory read.
        entries: One per reviewed manifest, available or not, in name order.
        invalid: ``(path, problem)`` for every manifest that could not be read at all — bad JSON,
            a wrong schema, a payload the contract refuses. Reported, never guessed at.
        drafts: Draft manifests present but not yet kept. Nothing trusts them; they are listed so
            an operator can see what is waiting for review.
        unmanifested: Artifacts with neither a manifest nor a draft — what ``scan`` would draft.
    """

    directory: Path
    entries: tuple[AdapterEntry, ...] = ()
    invalid: tuple[tuple[Path, str], ...] = ()
    drafts: tuple[Path, ...] = ()
    unmanifested: tuple[Path, ...] = ()

    @property
    def available(self) -> tuple[AdapterEntry, ...]:
        """The entries that may be registered."""
        return tuple(entry for entry in self.entries if entry.available)


def read_directory(directory: Path) -> DirectoryReading:
    """Read every reviewed manifest in ``directory`` and verify each against its artifact.

    An adapter is available only when its manifest parses, its artifact exists, and that
    artifact's content hashes to the digest the manifest records. Each of those failing is a
    *named* unavailability rather than an omission, because "the adapter I dropped in is not
    being used" is the operator confusion this whole design exists to prevent (ADR-0061's
    consequences).

    Args:
        directory: The configured adapter directory. A missing directory is an empty reading with
            no error: an operator who configured the path before creating it sees nothing
            registered, and ``doctor`` says why.

    Returns:
        The :class:`DirectoryReading`. Never raises for anything a file can do to it.
    """
    if not directory.is_dir():
        return DirectoryReading(directory=directory)

    entries: list[AdapterEntry] = []
    invalid: list[tuple[Path, str]] = []
    for manifest_path in sorted(directory.glob(f"*{MANIFEST_SUFFIX}")):
        if manifest_path.name.endswith(DRAFT_SUFFIX):
            continue
        entry, problem = _read_manifest(manifest_path, directory=directory)
        if entry is None:
            invalid.append((manifest_path, problem or "unreadable"))
        else:
            entries.append(entry)

    claimed = {entry.artifact_path for entry in entries}
    drafts = tuple(sorted(directory.glob(f"*{DRAFT_SUFFIX}")))
    drafted_stems = {path.name.removesuffix(DRAFT_SUFFIX) for path in drafts}
    unmanifested = tuple(
        artifact
        for artifact in sorted(directory.glob(f"*{ARTIFACT_SUFFIX}"))
        if artifact not in claimed and artifact.stem not in drafted_stems
    )
    return DirectoryReading(
        directory=directory,
        entries=tuple(sorted(entries, key=lambda entry: entry.name)),
        invalid=tuple(invalid),
        drafts=drafts,
        unmanifested=unmanifested,
    )


def _read_manifest(path: Path, *, directory: Path) -> tuple[AdapterEntry | None, str | None]:
    """Read and verify one manifest file. Returns ``(entry, problem)``; exactly one is set."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"could not be read: {exc}"
    try:
        envelope = load_envelope(raw, expect=MANIFEST_SCHEMA, supported=[MANIFEST_VERSION])
    except Exception as exc:  # noqa: BLE001 — every parse failure is reported, never raised
        return None, str(exc)
    try:
        manifest = AdapterManifestIn.model_validate(envelope.payload)
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "payload"
        return None, f"{location}: {first['msg']}"

    artifact_path = (directory / manifest.artifact_file).resolve()
    payload = dict(envelope.payload)
    common: dict[str, Any] = {
        "name": manifest.name,
        "manifest_path": path,
        "artifact_path": artifact_path,
        "artifact_sha256": manifest.artifact_sha256,
        "source_sha256": manifest.source_sha256,
        "base_model_name": manifest.base.provider_model_name,
        "base_artifact_digest": manifest.base.artifact_digest,
        "base_confidence": manifest.base.identity_confidence,
        "declared_capabilities": tuple(manifest.declared_capabilities),
        "data_classification": manifest.data_classification,
        "notes": manifest.notes,
        "payload": payload,
    }

    if not artifact_path.is_file():
        return (
            AdapterEntry(
                **common,
                available=False,
                unavailable_reason=(
                    f"the manifest names {manifest.artifact_file!r}, which is not a file in "
                    f"{directory}; the adapter is unavailable until the artifact returns or the "
                    "manifest is rescanned"
                ),
            ),
            None,
        )
    try:
        actual = sha256_of(artifact_path)
    except OSError as exc:
        return (
            AdapterEntry(
                **common,
                available=False,
                unavailable_reason=f"the artifact could not be hashed: {exc}",
            ),
            None,
        )
    if actual != manifest.artifact_sha256:
        return (
            AdapterEntry(
                **common,
                available=False,
                unavailable_reason=(
                    f"{manifest.artifact_file!r} hashes to {actual}, and the manifest records "
                    f"{manifest.artifact_sha256}. The content changed, so this is a different "
                    "adapter: rescan it rather than editing the digest, or the measurements "
                    "attached to the old hash would be re-attributed to new weights"
                ),
            ),
            None,
        )
    return AdapterEntry(**common, available=True, unavailable_reason=None), None


def registrations_from(entries: Sequence[AdapterEntry]) -> tuple[AdapterRegistration, ...]:
    """Convert available entries into ModelRack's registration type.

    The conversion lives here, in the application, because **ModelRack never reads the directory**
    (ADR-0061 rule 3): it receives registrations from the application constructing it, validates
    them and mounts them. ``.importlinter`` keeps that direction, and this function is the seam.

    Args:
        entries: Entries from :func:`read_directory`. Unavailable ones are skipped — an adapter
            whose artifact does not match its manifest must never be offered to a provider.

    Returns:
        One registration per available entry, in the order given.

    Raises:
        ValidationError: ModelRack refused a registration the manifest contract accepted. That
            would be a real disagreement between two validators over the same fields, and it must
            surface rather than be swallowed into "no adapters found".
    """
    from modelrack.adapters import AdapterRegistration

    return tuple(
        AdapterRegistration(
            name=entry.name,
            artifact_path=entry.artifact_path,
            artifact_sha256=entry.artifact_sha256,
            base_model_name=entry.base_model_name,
            data_classification=entry.data_classification,
            source_sha256=entry.source_sha256,
            base_artifact_digest=entry.base_artifact_digest,
        )
        for entry in entries
        if entry.available
    )


@dataclass(frozen=True, slots=True)
class DraftOutcome:
    """What one ``adapters scan`` pass wrote.

    Attributes:
        drafted: Draft manifests written, one per artifact that had none.
        skipped: ``(artifact, reason)`` for every artifact left alone — already manifested,
            already drafted, or unreadable. A scan that overwrote a kept manifest would destroy
            the review that makes the manifest trustworthy, so "already there" is always a skip.
    """

    drafted: tuple[Path, ...] = ()
    skipped: tuple[tuple[Path, str], ...] = ()


def draft_manifests(directory: Path, *, now: datetime | None = None) -> DraftOutcome:
    """Draft a manifest for every artifact in ``directory`` that has none (ADR-0061 rule 4).

    The scan does the tedious parts and leaves the assertions to a person. It hashes the artifact,
    reads a sibling ``adapter_config.json`` for the base name where one exists, and writes
    ``data_classification: confidential`` into every draft — so a reviewed value can only ever be
    *relaxed* on purpose (ADR-0065 rule 1). ``declared_capabilities`` is left empty, because a
    capability claim decides what FreeWeight benchmarks and what LoadCoach routes on, and a
    machine guessing it from a filename would put a fabricated claim into the evidence pipeline.

    The base is drafted at ``name_only`` confidence: ``adapter_config.json`` names its base by
    name, which is not a proof, and only a person can supply the digest that makes it one.

    Args:
        directory: The configured adapter directory.
        now: The instant to stamp; injected for deterministic tests.

    Returns:
        The :class:`DraftOutcome`. A draft is written with the ``.manifest.draft.json`` suffix and
        **nothing registers it** until a person reviews it and renames it to ``.manifest.json``.

    Raises:
        FileNotFoundError: ``directory`` does not exist. Unlike a read, a scan an operator asked
            for against a missing directory is a mistake worth naming.
    """
    if not directory.is_dir():
        message = f"adapter directory {directory} does not exist"
        raise FileNotFoundError(message)

    reading = read_directory(directory)
    manifested = {entry.artifact_path for entry in reading.entries}
    drafted_stems = {path.name.removesuffix(DRAFT_SUFFIX) for path in reading.drafts}

    drafted: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    for artifact in sorted(directory.glob(f"*{ARTIFACT_SUFFIX}")):
        if artifact.resolve() in manifested:
            skipped.append((artifact, "a reviewed manifest already names it"))
            continue
        if artifact.stem in drafted_stems:
            skipped.append((artifact, "a draft is already waiting for review"))
            continue
        try:
            digest = sha256_of(artifact)
        except OSError as exc:
            skipped.append((artifact, f"could not be hashed: {exc}"))
            continue
        target = directory / f"{artifact.stem}{DRAFT_SUFFIX}"
        target.write_text(
            _draft_document(artifact, digest=digest, now=now or datetime.now(UTC)),
            encoding="utf-8",
        )
        drafted.append(target)

    return DraftOutcome(drafted=tuple(drafted), skipped=tuple(skipped))


def _draft_document(artifact: Path, *, digest: str, now: datetime) -> str:
    """Render one draft manifest as a SetSpec envelope."""
    payload: dict[str, Any] = {
        "name": _draft_name(artifact),
        "artifact_file": artifact.name,
        "artifact_sha256": digest,
        "source_sha256": None,
        "base": {
            "provider_model_name": _base_name_from_peft(artifact) or "REVIEW-ME",
            "artifact_digest": None,
            "identity_confidence": IdentityConfidence.NAME_ONLY.value,
        },
        "declared_capabilities": [],
        "data_classification": DataClassification.CONFIDENTIAL.value,
        "format": "gguf",
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "notes": (
            "DRAFT — review every field, then rename this file to "
            f"{artifact.stem}{MANIFEST_SUFFIX} to register it. The base is named, not proved: "
            "supply base.artifact_digest (and set identity_confidence to 'digest') if you can "
            "verify it. declared_capabilities is empty because a capability claim is a person's "
            "assertion, not a scanner's guess. data_classification is 'confidential' until you "
            "relax it on purpose."
        ),
    }
    return dump_envelope(
        payload,
        schema=MANIFEST_SCHEMA,
        version=MANIFEST_VERSION,
        generator=GeneratorInfo(name="loadcoach", version=__version__),
        generated_at=now,
    )


def _draft_name(artifact: Path) -> str:
    """Derive a draft's ``name`` from its filename, in the shape the contract accepts."""
    candidate = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in artifact.stem.lower()
    ).strip("-")
    if not candidate or not candidate[0].isalpha():
        candidate = f"adapter-{candidate}".strip("-")
    return candidate[:64]


def _base_name_from_peft(artifact: Path) -> str | None:
    """Read a sibling ``adapter_config.json``'s base model name, when there is one.

    A PEFT checkpoint names its base by name, which is not a proof — hence ``name_only``. Absent
    or unreadable is not an error: the operator supplies the name during review.
    """
    config = artifact.parent / "adapter_config.json"
    if not config.is_file():
        return None
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    base = data.get("base_model_name_or_path")
    return base if isinstance(base, str) and base else None
