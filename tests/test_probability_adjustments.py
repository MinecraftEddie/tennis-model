"""Tests for probability_adjustments.shrink_toward_market."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tennis_model.probability_adjustments import shrink_toward_market


def test_1_underdog_shrinks():
    # model=0.60 odds=2.00 (market=0.50) → 0.7*0.60 + 0.3*0.50 = 0.57
    result = shrink_toward_market(0.60, 2.00)
    assert abs(result - 0.57) < 1e-9, f"expected 0.57, got {result}"


def test_2_already_at_market():
    # model=0.40 odds=2.50 (market=0.40) → final = 0.40 (no movement)
    result = shrink_toward_market(0.40, 2.50)
    assert abs(result - 0.40) < 1e-9, f"expected 0.40, got {result}"


def test_3_slight_underdog():
    # model=0.35 odds=3.00 (market=0.333) → 0.7*0.35 + 0.3*0.333 = 0.245 + 0.100 = 0.3449...
    expected = 0.7 * 0.35 + 0.3 * (1.0 / 3.00)
    result = shrink_toward_market(0.35, 3.00)
    assert abs(result - expected) < 1e-9, f"expected {expected:.6f}, got {result}"


def test_4_model_far_below_market():
    # model=0.30 odds=2.00 (market=0.50) → 0.7*0.30 + 0.3*0.50 = 0.21 + 0.15 = 0.36
    result = shrink_toward_market(0.30, 2.00)
    assert abs(result - 0.36) < 1e-9, f"expected 0.36, got {result}"


def test_5_strong_favorite():
    # model=0.80 odds=1.40 (market=0.714...) → 0.7*0.80 + 0.3*(1/1.40)
    expected = 0.7 * 0.80 + 0.3 * (1.0 / 1.40)
    result = shrink_toward_market(0.80, 1.40)
    assert abs(result - expected) < 1e-9, f"expected {expected:.6f}, got {result}"


def test_degenerate_odds_no_adjustment():
    # odds <= 1.0 should return model_prob unchanged
    result = shrink_toward_market(0.65, 1.00)
    assert abs(result - 0.65) < 1e-9


if __name__ == "__main__":
    test_1_underdog_shrinks()
    test_2_already_at_market()
    test_3_slight_underdog()
    test_4_model_far_below_market()
    test_5_strong_favorite()
    test_degenerate_odds_no_adjustment()
    print("All 6 tests passed.")
