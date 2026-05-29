from ghcr.cost import Prices, estimate_cost_usd, estimate_input_tokens
from ghcr.models import Usage


def test_cost_math():
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    prices = Prices(input_per_1m=0.435, output_per_1m=0.87)
    assert estimate_cost_usd(usage, prices) == 0.435 + 0.87


def test_cost_partial():
    usage = Usage(prompt_tokens=250_000, completion_tokens=500_000)
    prices = Prices(input_per_1m=0.28, output_per_1m=3.48)
    expected = 0.25 * 0.28 + 0.5 * 3.48
    assert abs(estimate_cost_usd(usage, prices) - expected) < 1e-9


def test_zero_usage_is_free():
    assert estimate_cost_usd(Usage(), Prices(1.0, 1.0)) == 0.0


def test_token_estimate_monotonic():
    assert estimate_input_tokens("") == 0
    assert estimate_input_tokens("abcd") == 1
    assert estimate_input_tokens("a" * 400) == 100
