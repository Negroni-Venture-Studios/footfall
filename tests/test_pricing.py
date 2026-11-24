import pytest

from footfall.tester.pricing import PRICES, PricingError, Usage, cost_usd


def test_cost_matches_hand_arithmetic() -> None:
    usage = Usage(
        input_tokens=1_000,
        output_tokens=500,
        cache_creation_input_tokens=2_000,
        cache_read_input_tokens=10_000,
    )
    # 0.002 + 0.005 + 0.005 + 0.001
    assert cost_usd("claude-sonnet-5-5", usage) == pytest.approx(0.013)


def test_opus_costs_double_sonnet() -> None:
    usage = Usage(3_000_000, 0, 0, 0)
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(12.0)
    assert cost_usd("claude-sonnet-5-5", usage) == pytest.approx(6.0)


def test_zero_usage_costs_nothing() -> None:
    assert cost_usd("claude-sonnet-5-5", Usage(0, 0, 0, 0)) == 0.0


def test_unknown_model_cannot_be_priced() -> None:
    with pytest.raises(PricingError, match="claude-imaginary-9"):
        cost_usd("claude-imaginary-9", Usage(1, 1, 1, 1))


def test_price_table_has_the_two_models() -> None:
    assert set(PRICES) == {"claude-sonnet-5-5", "claude-opus-5-5"}
