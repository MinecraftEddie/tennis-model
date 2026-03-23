"""Tests for confidence_caps.cap_data_availability."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tennis_model.confidence_caps import cap_data_availability


def test_below_cap():
    # Test 1: data = 0.40 → result 0.40 (no change)
    assert cap_data_availability(0.40) == 0.40


def test_at_cap():
    # Test 2: data = 0.55 → result 0.55 (boundary, no change)
    assert cap_data_availability(0.55) == 0.55


def test_above_cap():
    # Test 3: data = 0.90 → result 0.55 (capped)
    assert cap_data_availability(0.90) == 0.55


def test_total_score_both_dominated():
    # Test 4: old_total=0.90, data=0.90, other=0.00 → new_total=0.55
    old_total = 0.90
    data = 0.90
    capped = cap_data_availability(data)
    new_total = old_total - data + capped
    assert abs(new_total - 0.55) < 1e-9


def test_total_score_partial():
    # Test 5: old_total=1.00, data=0.90, other=0.10 → new_total=0.65
    old_total = 1.00
    data = 0.90
    capped = cap_data_availability(data)
    new_total = old_total - data + capped
    assert abs(new_total - 0.65) < 1e-9


if __name__ == "__main__":
    test_below_cap()
    test_at_cap()
    test_above_cap()
    test_total_score_both_dominated()
    test_total_score_partial()
    print("All 5 tests passed.")
