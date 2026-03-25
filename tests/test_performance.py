"""
tests/test_performance.py
==========================
Tests for tracking/performance.py — performance summary (Step 3 post-P6).

Coverage:
  1. Empty summary
  2. Single win
  3. Single loss
  4. Mixed outcomes
  5. Win-rate calculation
  6. ROI calculation
  7. load_and_summarize reads existing outcomes
  8. Guard-fous: 0 outcomes / 0 stake
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch

from tennis_model.tracking.performance import (
    PerformanceSummary,
    summarize_outcomes,
    load_and_summarize,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _outcome(result="win", odds=1.80, stake_units=1.0, profit_units=None):
    """Build a minimal outcome dict matching save_outcome_record format."""
    if profit_units is None:
        if result == "win":
            profit_units = round(stake_units * (odds - 1), 4)
        else:
            profit_units = round(-stake_units, 4)
    return {
        "date": "2026-03-24",
        "match_id": "2026-03-24_test",
        "player_a": "A",
        "player_b": "B",
        "pick_side": "A",
        "winner": "A" if result == "win" else "B",
        "result": result,
        "odds": odds,
        "stake_units": stake_units,
        "profit_units": profit_units,
        "settled_at": "2026-03-24T12:00:00Z",
    }


# ── 1. Empty summary ────────────────────────────────────────────────────────

def test_summarize_empty():
    s = summarize_outcomes([])
    assert s.total_picks == 0
    assert s.settled_picks == 0
    assert s.wins == 0
    assert s.losses == 0
    assert s.win_rate == 0.0
    assert s.roi == 0.0
    assert s.total_stake_units == 0.0
    assert s.total_profit_units == 0.0
    assert s.average_odds == 0.0
    assert s.average_stake_units == 0.0


# ── 2. Single win ───────────────────────────────────────────────────────────

def test_summarize_single_win():
    s = summarize_outcomes([_outcome(result="win", odds=1.80, stake_units=1.0)])
    assert s.total_picks == 1
    assert s.settled_picks == 1
    assert s.wins == 1
    assert s.losses == 0
    assert s.win_rate == pytest.approx(1.0)
    assert s.total_stake_units == pytest.approx(1.0)
    assert s.total_profit_units == pytest.approx(0.80, abs=1e-4)
    assert s.roi == pytest.approx(0.80, abs=1e-4)
    assert s.average_odds == pytest.approx(1.80)
    assert s.average_stake_units == pytest.approx(1.0)


# ── 3. Single loss ──────────────────────────────────────────────────────────

def test_summarize_single_loss():
    s = summarize_outcomes([_outcome(result="loss", odds=1.80, stake_units=1.0)])
    assert s.total_picks == 1
    assert s.settled_picks == 1
    assert s.wins == 0
    assert s.losses == 1
    assert s.win_rate == pytest.approx(0.0)
    assert s.total_profit_units == pytest.approx(-1.0, abs=1e-4)
    assert s.roi == pytest.approx(-1.0, abs=1e-4)


# ── 4. Mixed outcomes ───────────────────────────────────────────────────────

def test_summarize_mixed():
    outcomes = [
        _outcome(result="win",  odds=1.80, stake_units=1.0),   # +0.80
        _outcome(result="loss", odds=2.10, stake_units=1.0),   # -1.00
        _outcome(result="win",  odds=2.50, stake_units=0.5),   # +0.75
    ]
    s = summarize_outcomes(outcomes)
    assert s.total_picks == 3
    assert s.settled_picks == 3
    assert s.wins == 2
    assert s.losses == 1
    assert s.total_stake_units == pytest.approx(2.5)
    assert s.total_profit_units == pytest.approx(0.55, abs=1e-4)


# ── 5. Win-rate calculation ─────────────────────────────────────────────────

def test_win_rate_two_of_three():
    outcomes = [
        _outcome(result="win"),
        _outcome(result="win"),
        _outcome(result="loss"),
    ]
    s = summarize_outcomes(outcomes)
    assert s.win_rate == pytest.approx(2.0 / 3.0, abs=1e-4)


def test_win_rate_all_losses():
    outcomes = [_outcome(result="loss") for _ in range(5)]
    s = summarize_outcomes(outcomes)
    assert s.win_rate == pytest.approx(0.0)
    assert s.losses == 5


def test_win_rate_all_wins():
    outcomes = [_outcome(result="win") for _ in range(4)]
    s = summarize_outcomes(outcomes)
    assert s.win_rate == pytest.approx(1.0)


# ── 6. ROI calculation ──────────────────────────────────────────────────────

def test_roi_positive():
    # 3 wins at 1.80 for 1u each: profit = 3*0.80 = 2.40, stake = 3.0
    outcomes = [_outcome(result="win", odds=1.80, stake_units=1.0) for _ in range(3)]
    s = summarize_outcomes(outcomes)
    assert s.roi == pytest.approx(2.40 / 3.0, abs=1e-4)


def test_roi_negative():
    # 3 losses at 1.80 for 1u each: profit = -3.0, stake = 3.0
    outcomes = [_outcome(result="loss", odds=1.80, stake_units=1.0) for _ in range(3)]
    s = summarize_outcomes(outcomes)
    assert s.roi == pytest.approx(-1.0, abs=1e-4)


def test_roi_breakeven():
    # 1 win at 2.00 (+1.0) and 1 loss (-1.0) → profit=0, stake=2
    outcomes = [
        _outcome(result="win",  odds=2.00, stake_units=1.0),
        _outcome(result="loss", odds=2.00, stake_units=1.0),
    ]
    s = summarize_outcomes(outcomes)
    assert s.roi == pytest.approx(0.0, abs=1e-4)


# ── 7. load_and_summarize reads existing outcomes ────────────────────────────

def test_load_and_summarize(tmp_path):
    import json
    # Write a fake outcomes JSONL
    outcomes_dir = str(tmp_path)
    path = os.path.join(outcomes_dir, "2026-03-24.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps(_outcome(result="win", odds=1.90, stake_units=1.0)) + "\n")
        f.write(json.dumps(_outcome(result="loss", odds=2.00, stake_units=1.0)) + "\n")

    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir):
        s = load_and_summarize("2026-03-24")

    assert s.total_picks == 2
    assert s.settled_picks == 2
    assert s.wins == 1
    assert s.losses == 1
    assert s.total_profit_units == pytest.approx(-0.10, abs=1e-4)


def test_load_and_summarize_missing_date(tmp_path):
    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", str(tmp_path)):
        s = load_and_summarize("2099-01-01")
    assert s.total_picks == 0
    assert s.roi == 0.0


# ── 8. Guard-fous: 0 outcomes / 0 stake ─────────────────────────────────────

def test_zero_stake_no_division_error():
    """Outcome with 0 stake should not cause ZeroDivisionError."""
    outcomes = [_outcome(result="win", odds=1.80, stake_units=0.0, profit_units=0.0)]
    s = summarize_outcomes(outcomes)
    assert s.total_picks == 1
    assert s.roi == 0.0
    assert s.average_stake_units == 0.0


def test_zero_odds_no_division_error():
    """Outcome with 0 odds should not affect average_odds."""
    outcomes = [_outcome(result="win", odds=0.0, stake_units=1.0, profit_units=0.0)]
    s = summarize_outcomes(outcomes)
    assert s.average_odds == 0.0


# ── 9. Average calculations ─────────────────────────────────────────────────

def test_average_odds_correct():
    outcomes = [
        _outcome(odds=1.80),
        _outcome(odds=2.20),
        _outcome(odds=3.00),
    ]
    s = summarize_outcomes(outcomes)
    assert s.average_odds == pytest.approx((1.80 + 2.20 + 3.00) / 3, abs=1e-4)


def test_average_stake_correct():
    outcomes = [
        _outcome(stake_units=1.0),
        _outcome(stake_units=0.5),
        _outcome(stake_units=2.0),
    ]
    s = summarize_outcomes(outcomes)
    assert s.average_stake_units == pytest.approx((1.0 + 0.5 + 2.0) / 3, abs=1e-4)
