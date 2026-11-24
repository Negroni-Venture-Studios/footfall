"""What a model call costs, so that every run can be held to a cost ceiling."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


class PricingError(KeyError):
    """A model has no price here: Footfall never asks for it, and charges it the dearest rates."""

    def __str__(self) -> str:
        # KeyError would show the message in quotes.
        return str(self.args[0]) if self.args else ""


@dataclass(frozen=True)
class Usage:
    """Tokens one model call used.

    `cache_creation_input_tokens` counts every token written to the cache. The part of it
    written for an hour, which costs more than a five-minute write, is
    `cache_write_1h_input_tokens`.
    """

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    cache_write_1h_input_tokens: int = 0
    # Where the API ran the call, as `usage.inference_geo` reports it; None is worldwide.
    inference_geo: str | None = None


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens. `cache_write` is the five-minute rate."""

    input: float
    output: float
    cache_write: float
    cache_read: float
    cache_write_1h: float


# From Anthropic's pricing page, https://platform.claude.com/docs/en/about-claude/pricing,
# checked on 24 November 2026. Each rate is stored as published, never worked out from
# another. Cache reads cost a twentieth of input on Sonnet 5.5 and Opus 5.5 and a tenth on
# the others. None of these models charges more for a long prompt.
#
# Footfall asks only for the 5.5 models. The others are their fallbacks, which serve a turn
# the model asked for declines, and which Footfall pays for: Sonnet 5.5 falls back to
# Sonnet 5, and Opus 5.5 to Opus 5 or Opus 4.8. Sonnet 5's $2 and $10 began as an
# introductory price and became permanent on 11 August 2026.
PRICES: Final = MappingProxyType(
    {
        "claude-sonnet-5-5": Price(2.00, 10.00, 2.50, 0.10, 4.00),
        "claude-opus-5-5": Price(4.00, 20.00, 5.00, 0.20, 8.00),
        "claude-sonnet-5": Price(2.00, 10.00, 2.50, 0.20, 4.00),
        "claude-opus-5": Price(5.00, 25.00, 6.25, 0.50, 10.00),
        "claude-opus-4-8": Price(5.00, 25.00, 6.25, 0.50, 10.00),
    }
)

# The highest rate of each kind in the table, for a model the API answers as that has no
# price: a cost ceiling should overcharge rather than undercharge.
DEAREST: Final = Price(
    input=max(p.input for p in PRICES.values()),
    output=max(p.output for p in PRICES.values()),
    cache_write=max(p.cache_write for p in PRICES.values()),
    cache_read=max(p.cache_read for p in PRICES.values()),
    cache_write_1h=max(p.cache_write_1h for p in PRICES.values()),
)

# A call run only in the United States costs 1.1 times as much in every kind of token.
# Footfall does not ask for it, but a reply that says so is charged for it. A region not
# listed here is charged at the dearest multiplier.
GEO_MULTIPLIERS: Final = MappingProxyType({"global": 1.0, "us": 1.1})


def cost_usd(model: str, usage: Usage) -> float:
    price = PRICES.get(model)
    if price is None:
        raise PricingError(f"no price for model {model!r}; add it to PRICES to run it")
    return _cost(price, usage)


def dearest_cost_usd(usage: Usage) -> float:
    """What `usage` costs at the highest rate of each kind in the table."""
    return _cost(DEAREST, usage)


def _cost(price: Price, usage: Usage) -> float:
    # More one-hour tokens than written in all would be the API's error; price them all.
    one_hour = usage.cache_write_1h_input_tokens
    five_minutes = max(usage.cache_creation_input_tokens - one_hour, 0)
    geo = usage.inference_geo or "global"
    multiplier = GEO_MULTIPLIERS.get(geo, max(GEO_MULTIPLIERS.values()))
    return (
        multiplier
        * (
            usage.input_tokens * price.input
            + usage.output_tokens * price.output
            + five_minutes * price.cache_write
            + one_hour * price.cache_write_1h
            + usage.cache_read_input_tokens * price.cache_read
        )
        / 1_000_000
    )
