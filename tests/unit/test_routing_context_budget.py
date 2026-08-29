"""Context budgeting (routing §9): fit, reduce where permitted, or reject with numbers."""

from __future__ import annotations

import pytest

from loadcoach.domain.routing.context_budget import (
    CHARS_PER_TOKEN,
    SAFETY_MARGIN_TOKENS,
    budget_context,
    estimate_input_tokens,
)


def test_a_request_that_fits_is_unchanged() -> None:
    budget = budget_context(
        estimated_input_tokens=1000,
        max_output_tokens=2048,
        served_context=32768,
        served_context_source="configured",
    )
    assert budget.fits is True
    assert budget.reduced is False
    assert budget.max_output_tokens == 2048
    assert budget.required_context == 1000 + 2048 + SAFETY_MARGIN_TOKENS
    assert budget.shortfall_tokens == 0


def test_output_tokens_are_reduced_only_where_the_profile_permits_it() -> None:
    budget = budget_context(
        estimated_input_tokens=7000,
        max_output_tokens=4096,
        served_context=8192,
        served_context_source="configured",
        min_output_tokens=256,
    )
    assert budget.fits is True
    assert budget.reduced is True
    assert budget.max_output_tokens == 8192 - 7000 - SAFETY_MARGIN_TOKENS
    assert budget.max_output_tokens >= 256
    assert budget.requested_output_tokens == 4096
    assert budget.required_context <= 8192


def test_without_a_floor_a_request_that_does_not_fit_is_rejected_with_numbers() -> None:
    budget = budget_context(
        estimated_input_tokens=7000,
        max_output_tokens=4096,
        served_context=8192,
        served_context_source="configured",
    )
    assert budget.fits is False
    assert budget.reduced is False
    assert budget.max_output_tokens == 4096
    assert budget.required_context == 7000 + 4096 + SAFETY_MARGIN_TOKENS
    assert budget.shortfall_tokens == budget.required_context - 8192
    assert budget.shortfall_tokens > 0


def test_a_request_below_the_floor_is_rejected_rather_than_shortened_past_it() -> None:
    budget = budget_context(
        estimated_input_tokens=8000,
        max_output_tokens=4096,
        served_context=8192,
        served_context_source="configured",
        min_output_tokens=512,
    )
    assert budget.fits is False
    assert budget.reduced is False


def test_the_input_is_never_shortened_to_make_a_request_fit() -> None:
    """routing §9: truncating the caller's input is never done here, at any size."""
    for served in (8192, 4096, 1024, 128):
        budget = budget_context(
            estimated_input_tokens=7000,
            max_output_tokens=1024,
            served_context=served,
            served_context_source="configured",
            min_output_tokens=1,
        )
        assert budget.estimated_input_tokens == 7000


def test_the_advertised_maximum_never_enters_the_arithmetic() -> None:
    """A model advertising 131 072 tokens served 4 096 is budgeted against 4 096."""
    budget = budget_context(
        estimated_input_tokens=20000,
        max_output_tokens=1024,
        served_context=4096,
        served_context_source="configured",
    )
    assert budget.fits is False
    assert budget.served_context == 4096


def test_the_character_estimate_records_its_ratio() -> None:
    assert estimate_input_tokens("a" * 400) == 100
    assert estimate_input_tokens("a" * 401) == 101
    budget = budget_context(
        estimated_input_tokens=estimate_input_tokens("a" * 400),
        max_output_tokens=100,
        served_context=8192,
        served_context_source="reported",
        input_estimate_source="character_estimate",
        chars_per_token=CHARS_PER_TOKEN,
    )
    assert budget.input_estimate_source == "character_estimate"
    assert budget.chars_per_token == CHARS_PER_TOKEN
    assert budget.as_json()["chars_per_token"] == CHARS_PER_TOKEN


def test_a_non_positive_ratio_is_refused() -> None:
    with pytest.raises(ValueError, match="chars_per_token must be positive"):
        estimate_input_tokens("text", chars_per_token=0)
