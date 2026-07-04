from ghcr.cost import Prices, estimate_cost_usd, estimate_input_tokens, per_chunk_diff_budget
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


def test_chunk_budget_token_cap_binds():
    # 4 lenses, 250k cap, 10k overhead: token room 240k → 0.9*4*240k/4 = 216k bytes,
    # below the 400k byte cap — the token gate is the binding constraint.
    got = per_chunk_diff_budget(400_000, 250_000, 4, 10_000)
    assert got == int(0.9 * 4 * 240_000 / 4)
    assert got < 400_000


def test_chunk_budget_byte_cap_binds():
    # Tiny byte cap with a huge token cap → byte cap wins.
    assert per_chunk_diff_budget(1_000, 10_000_000, 4, 0) == 1_000


def test_chunk_budget_pathological_config_nonpositive():
    # Overhead alone exceeds the token cap → <= 0, caller must skip pre-spend.
    assert per_chunk_diff_budget(400_000, 1_000, 4, 5_000) <= 0
