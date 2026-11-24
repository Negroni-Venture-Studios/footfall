import pytest

from footfall.tester.pricing import DEAREST, PRICES, Price, PricingError, Usage, cost_usd


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


def test_price_table_has_the_two_models_and_their_fallbacks() -> None:
    assert set(PRICES) == {
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "claude-sonnet-5",
        "claude-opus-5",
        "claude-opus-4-8",
    }


def test_published_prices() -> None:
    # Input, output, five-minute cache write, cache read, one-hour cache write.
    assert PRICES["claude-sonnet-5-5"] == Price(2.00, 10.00, 2.50, 0.10, 4.00)
    assert PRICES["claude-opus-5-5"] == Price(4.00, 20.00, 5.00, 0.20, 8.00)
    assert PRICES["claude-sonnet-5"] == Price(2.00, 10.00, 2.50, 0.20, 4.00)
    assert PRICES["claude-opus-5"] == Price(5.00, 25.00, 6.25, 0.50, 10.00)
    assert PRICES["claude-opus-4-8"] == Price(5.00, 25.00, 6.25, 0.50, 10.00)


def test_cache_prices_follow_the_published_ratios() -> None:
    # Writes cost 1.25 times input for five minutes and twice input for an hour. Reads
    # cost a tenth of input, except on the 5.5 models, which cut them to a twentieth.
    reads = {"claude-sonnet-5-5": 0.05, "claude-opus-5-5": 0.05}
    for model, price in PRICES.items():
        assert price.cache_write == pytest.approx(price.input * 1.25), model
        assert price.cache_write_1h == pytest.approx(price.input * 2.0), model
        assert price.cache_read == pytest.approx(price.input * reads.get(model, 0.1)), model


def test_sonnet_costs_less_than_opus_and_a_cache_read_less_than_input() -> None:
    fields = ("input", "output", "cache_write", "cache_read", "cache_write_1h")
    for sonnet, opus in [
        ("claude-sonnet-5-5", "claude-opus-5-5"),
        ("claude-sonnet-5", "claude-opus-5"),
        ("claude-sonnet-5", "claude-opus-4-8"),
    ]:
        for name in fields:
            assert getattr(PRICES[sonnet], name) < getattr(PRICES[opus], name), (sonnet, name)
    for model, price in PRICES.items():
        assert price.cache_read < price.input, model


def test_the_dearest_rates_are_the_highest_of_each_kind() -> None:
    assert Price(5.00, 25.00, 6.25, 0.50, 10.00) == DEAREST


def test_us_only_inference_costs_a_tenth_more() -> None:
    usage = Usage(1_000_000, 1_000_000, 1_000_000, 1_000_000, cache_write_1h_input_tokens=400_000)
    base = cost_usd("claude-opus-5-5", usage)
    assert base == pytest.approx(4 + 20 + 0.6 * 5 + 0.4 * 8 + 0.2)
    us = Usage(**{**usage.__dict__, "inference_geo": "us"})
    assert cost_usd("claude-opus-5-5", us) == pytest.approx(base * 1.1)
    worldwide = Usage(**{**usage.__dict__, "inference_geo": "global"})
    assert cost_usd("claude-opus-5-5", worldwide) == pytest.approx(base)
    # A region with no known rate is charged as the dearest one.
    elsewhere = Usage(**{**usage.__dict__, "inference_geo": "mars"})
    assert cost_usd("claude-opus-5-5", elsewhere) == pytest.approx(base * 1.1)


def test_one_hour_cache_writes_cost_more() -> None:
    usage = Usage(0, 0, 1_000_000, 0, cache_write_1h_input_tokens=400_000)
    # 600,000 at $2.50 and 400,000 at $4.00 per million.
    assert cost_usd("claude-sonnet-5-5", usage) == pytest.approx(1.5 + 1.6)
    # More one-hour tokens than written in all is priced as all one-hour, never less.
    odd = Usage(0, 0, 100, 0, cache_write_1h_input_tokens=1_000_000)
    assert cost_usd("claude-sonnet-5-5", odd) == pytest.approx(4.0)


def test_prices_are_read_only() -> None:
    with pytest.raises(TypeError):
        PRICES["claude-x"] = PRICES["claude-sonnet-5-5"]  # type: ignore[index]


def test_pricing_error_message_is_not_quoted() -> None:
    message = str(PricingError("no price for model 'x'"))
    assert message == "no price for model 'x'"
