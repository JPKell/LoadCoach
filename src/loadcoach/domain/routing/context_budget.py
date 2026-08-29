"""loadcoach.domain.routing.context_budget — fitting a request into the context that is served.

Routing §9::

    required_context = estimated_input_tokens + max_output_tokens + safety_margin
    usable_context   = served_context      (never descriptor.max_context)

If the request does not fit, LoadCoach reduces ``max_output_tokens`` down to the profile's floor
where the profile permits it, and otherwise rejects with the numbers. **Truncating the caller's
input is never done here at all** — not silently, and not as a fallback; routing §9 makes it an
explicit request option, which no phase has yet introduced.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

__all__ = [
    "CHARS_PER_TOKEN",
    "SAFETY_MARGIN_TOKENS",
    "ContextBudget",
    "budget_context",
    "estimate_input_tokens",
]

CHARS_PER_TOKEN: Final = 4.0
"""The documented character-based ratio used when no provider tokenizer is available (routing §9:
"otherwise a documented character-based estimate with its ratio recorded"). Every budget records
the ratio it used, so an estimate is never mistaken for a count."""

SAFETY_MARGIN_TOKENS: Final = 256
"""Headroom above input plus output: chat templating, a system preamble the provider adds, and
tokenizer disagreement between this estimate and the runtime's own. Small enough not to reject
work that would fit, large enough that a template does not push a fitted request over."""


def estimate_input_tokens(text: str, *, chars_per_token: float = CHARS_PER_TOKEN) -> int:
    """Estimate a prompt's token count from its length.

    The fallback for a provider that exposes no tokenizer. Deliberately a separate function from
    the budget, so a caller that *does* have a count passes it instead of an estimate and the
    difference stays visible.

    Args:
        text: The prompt, exactly as the caller sent it.
        chars_per_token: The ratio to divide by.

    Returns:
        The estimate, rounded up — an under-estimate is what overflows a context window.
    """
    if chars_per_token <= 0:
        message = "chars_per_token must be positive"
        raise ValueError(message)
    return math.ceil(len(text) / chars_per_token)


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Whether a request fits the context it will be served, and at what output length.

    Attributes:
        fits: Whether the request can be executed at all.
        estimated_input_tokens: What the caller supplied or what was estimated.
        max_output_tokens: The output allowance **after** any permitted reduction.
        requested_output_tokens: The profile's original allowance.
        reduced: Whether the allowance was reduced to make the request fit.
        safety_margin_tokens: The margin included in ``required_context``.
        required_context: ``input + output + margin``, after any reduction.
        served_context: What the candidate will actually be served.
        served_context_source: ``"configured"``, ``"reported"`` or ``"assumed"``.
        input_estimate_source: ``"caller"``, ``"tokenizer"`` or ``"character_estimate"``.
        chars_per_token: The ratio, recorded whenever the estimate produced the number.
        shortfall_tokens: How many tokens over the limit the request is, when it does not fit.
    """

    fits: bool
    estimated_input_tokens: int
    max_output_tokens: int
    requested_output_tokens: int
    reduced: bool
    safety_margin_tokens: int
    required_context: int
    served_context: int
    served_context_source: str
    input_estimate_source: str
    chars_per_token: float | None = None
    shortfall_tokens: int = 0

    def as_json(self) -> dict[str, object]:
        """Return the mapping the explanation and a rejection detail carry."""
        return {
            "fits": self.fits,
            "estimated_input_tokens": self.estimated_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "requested_output_tokens": self.requested_output_tokens,
            "reduced": self.reduced,
            "safety_margin_tokens": self.safety_margin_tokens,
            "required_context": self.required_context,
            "served_context": self.served_context,
            "served_context_source": self.served_context_source,
            "input_estimate_source": self.input_estimate_source,
            "chars_per_token": self.chars_per_token,
            "shortfall_tokens": self.shortfall_tokens,
        }


def budget_context(
    *,
    estimated_input_tokens: int,
    max_output_tokens: int,
    served_context: int,
    served_context_source: str,
    min_output_tokens: int | None = None,
    safety_margin_tokens: int = SAFETY_MARGIN_TOKENS,
    input_estimate_source: str = "caller",
    chars_per_token: float | None = None,
) -> ContextBudget:
    """Fit a request into ``served_context``, reducing the output allowance where permitted.

    ``served_context`` is the only limit consulted. The descriptor's advertised maximum never
    enters this arithmetic (ADR-0023 §4): a model that advertises 131 072 tokens and will be
    served 4 096 must be measured against 4 096, or the provider silently truncates the caller's
    input and returns a confidently wrong answer.

    Args:
        estimated_input_tokens: The prompt's size, counted or estimated.
        max_output_tokens: The task profile's output allowance.
        served_context: What this candidate will be served, in tokens.
        served_context_source: Where that number came from.
        min_output_tokens: The profile's floor. ``None`` means the profile does not permit a
            reduction, and a request that does not fit is rejected rather than shortened.
        safety_margin_tokens: Headroom above input plus output.
        input_estimate_source: How ``estimated_input_tokens`` was obtained.
        chars_per_token: The ratio, when a character estimate produced the number.

    Returns:
        The :class:`ContextBudget`. ``fits=False`` carries ``shortfall_tokens`` so the caller can
        reject with numbers rather than a bare refusal.
    """
    required = estimated_input_tokens + max_output_tokens + safety_margin_tokens
    if required <= served_context:
        return ContextBudget(
            fits=True,
            estimated_input_tokens=estimated_input_tokens,
            max_output_tokens=max_output_tokens,
            requested_output_tokens=max_output_tokens,
            reduced=False,
            safety_margin_tokens=safety_margin_tokens,
            required_context=required,
            served_context=served_context,
            served_context_source=served_context_source,
            input_estimate_source=input_estimate_source,
            chars_per_token=chars_per_token,
        )

    room = served_context - estimated_input_tokens - safety_margin_tokens
    if min_output_tokens is not None and room >= min_output_tokens:
        reduced_output = min(max_output_tokens, room)
        return ContextBudget(
            fits=True,
            estimated_input_tokens=estimated_input_tokens,
            max_output_tokens=reduced_output,
            requested_output_tokens=max_output_tokens,
            reduced=True,
            safety_margin_tokens=safety_margin_tokens,
            required_context=estimated_input_tokens + reduced_output + safety_margin_tokens,
            served_context=served_context,
            served_context_source=served_context_source,
            input_estimate_source=input_estimate_source,
            chars_per_token=chars_per_token,
        )

    return ContextBudget(
        fits=False,
        estimated_input_tokens=estimated_input_tokens,
        max_output_tokens=max_output_tokens,
        requested_output_tokens=max_output_tokens,
        reduced=False,
        safety_margin_tokens=safety_margin_tokens,
        required_context=required,
        served_context=served_context,
        served_context_source=served_context_source,
        input_estimate_source=input_estimate_source,
        chars_per_token=chars_per_token,
        shortfall_tokens=required - served_context,
    )
