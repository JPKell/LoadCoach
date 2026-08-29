"""loadcoach.services.prompts — the prompt pack LoadCoach originates.

Two prompts, and only two, are LoadCoach's own (spec §9): the corrective instruction appended on a
structured-output retry, and the re-probe a circuit breaker issues. Everything else a model sees
is the caller's text, passed through unmodified.

The machinery is :mod:`setspec.prompts`; this module is the one-function shim its own adoption
checklist describes — it supplies the pack root LoadCoach ships, so the fourteen call sites do not
each have to know where it lives.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Final

from setspec.prompts import load_pack

if TYPE_CHECKING:
    from setspec.prompts import PromptLibrary, RenderedPrompt

__all__ = ["PACK_ROOT", "STRUCTURED_OUTPUT_RETRY", "library", "render_corrective_retry"]

PACK_ROOT: Final = Path(__file__).resolve().parent.parent / "prompts"
"""Where LoadCoach's own pack lives. Package data, present in the built wheel."""

STRUCTURED_OUTPUT_RETRY: Final = "execution.structured_output.retry"
"""The one prompt this phase applies to a caller's job."""


@lru_cache(maxsize=1)
def library() -> PromptLibrary:
    """Return the loaded prompt pack, reading it once per process.

    Returns:
        The library. Loading validates every record against the manifest's hashes, so a prompt
        edited without regenerating the manifest fails here rather than silently changing what a
        model was asked.

    Raises:
        PromptPackInvalid: A record is malformed, or the manifest does not describe the pack.
    """
    return load_pack(PACK_ROOT)


def render_corrective_retry(*, problems: str, schema: str, previous_output: str) -> RenderedPrompt:
    """Render the structured-output corrective instruction.

    Args:
        problems: The failing checks, one per line, each naming a field path and what was wrong.
        schema: The JSON Schema the output must satisfy, as text.
        previous_output: The model's own previous answer, so the correction preserves its
            substance rather than regenerating it.

    Returns:
        The rendered prompt, carrying the ``prompt_id``, ``version`` and ``sha256`` that get
        recorded on the attempt that used it.
    """
    return library().render(
        STRUCTURED_OUTPUT_RETRY,
        {"problems": problems, "schema": schema, "previous_output": previous_output},
    )
