"""What a model call costs, so that every run can be held to a cost ceiling."""

from dataclasses import dataclass
from typing import Final


class PricingError(KeyError):
    """A model has no price here, so Footfall will not run it."""


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens."""

    input: float
    output: float
    cache_write: float
    cache_read: float


# Prices as published on 3 November 2026.
PRICES: Final = {
    "claude-sonnet-5-5": Price(2.00, 10.00, 2.50, 0.10),
    "claude-opus-5-5": Price(4.00, 20.00, 5.00, 0.20),
}


def cost_usd(model: str, usage: Usage) -> float:
    price = PRICES.get(model)
    if price is None:
        raise PricingError(f"no price for model {model!r}; add it to PRICES to run it")
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_creation_input_tokens * price.cache_write
        + usage.cache_read_input_tokens * price.cache_read
    ) / 1_000_000
