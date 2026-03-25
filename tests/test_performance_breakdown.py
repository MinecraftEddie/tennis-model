"""
tests/test_performance_breakdown.py
====================================
Tests for tracking/performance_breakdown.py — Step 4 post-P6.

Coverage:
  1. Breakdown vide
  2. Jointure pick + outcome correcte
  3. Outcome orphelin ignoré
  4. Breakdown by_profile_quality
  5. Breakdown by_final_status
  6. Breakdown by_is_dry_run
  7. ROI correct dans chaque bucket
  8. total_picks vs settled_picks quand certains picks non settlés
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch

from tennis_model.tracking.performance_breakdown import (
    PerformanceBucket,
    PerformanceBreakdown,
    summarize_joined_records,
    load_and_summarize_breakdown,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pick(match_id="m1", pick_side="A", odds=1.80, stake_units=1.0,
          profile_quality_a="full", profile_quality_b="full",
          final_status="PICK_ALERT_SENT", is_dry_run=False):
    return {
        "date": "2026-03-24",
        "match_id": match_id,
        "player_a": "A",
        "player_b": "B",
        "pick_side": pick_side,
        "odds": odds,
        "stake_units": stake_units,
        "profile_quality_a": profile_quality_a,
        "profile_quality_b": profile_quality_b,
        "evaluator_status": "PICK",
        "final_status": final_status,
        "reason_codes": ["PICK_APPROVED"],
        "confidence": "HIGH",
        "ev": 0.12,
        "is_dry_run": is_dry_run,
        "created_at": "2026-03-24T10:00:00Z",
    }


def _outcome(match_id="m1", result="win", odds=1.80, stake_units=1.0,
             profit_units=None):
    if profit_units is None:
        profit_units = round(stake_units * (odds - 1), 4) if result == "win" else round(-stake_units, 4)
    return {
        "date": "2026-03-24",
        "match_id": match_id,
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


# ── 1. Breakdown vide ───────────────────────────────────────────────────────

def test_empty_breakdown():
    bd = summarize_joined_records([], [])
    assert bd.by_profile_quality == {}
    assert bd.by_final_status == {}
    assert bd.by_is_dry_run == {}


# ── 2. Jointure pick + outcome correcte ─────────────────────────────────────

def test_join_pick_outcome():
    picks = [_pick(match_id="m1", odds=1.80, stake_units=1.0)]
    outcomes = [_outcome(match_id="m1", result="win", odds=1.80, stake_units=1.0)]

    bd = summarize_joined_records(picks, outcomes)
    b = bd.by_profile_quality["full"]

    assert b.total_picks == 1
    assert b.settled_picks == 1
    assert b.wins == 1
    assert b.losses == 0
    assert b.total_profit_units == pytest.approx(0.80, abs=1e-4)


# ── 3. Outcome orphelin ignoré ──────────────────────────────────────────────

def test_orphan_outcome_ignored():
    picks = [_pick(match_id="m1")]
    outcomes = [
        _outcome(match_id="m1", result="win"),
        _outcome(match_id="m_orphan", result="loss"),  # no matching pick
    ]
    bd = summarize_joined_records(picks, outcomes)
    b = bd.by_profile_quality["full"]
    assert b.total_picks == 1
    assert b.settled_picks == 1
    assert b.wins == 1


# ── 4. Breakdown by_profile_quality ──────────────────────────────────────────

def test_by_profile_quality():
    picks = [
        _pick(match_id="m1", pick_side="A", profile_quality_a="full"),
        _pick(match_id="m2", pick_side="A", profile_quality_a="degraded"),
        _pick(match_id="m3", pick_side="B", profile_quality_b="degraded"),
    ]
    outcomes = [
        _outcome(match_id="m1", result="win"),
        _outcome(match_id="m2", result="loss"),
        _outcome(match_id="m3", result="win", odds=2.10, stake_units=1.0),
    ]
    bd = summarize_joined_records(picks, outcomes)

    assert "full" in bd.by_profile_quality
    assert "degraded" in bd.by_profile_quality

    full = bd.by_profile_quality["full"]
    assert full.total_picks == 1
    assert full.wins == 1
    assert full.losses == 0

    degraded = bd.by_profile_quality["degraded"]
    assert degraded.total_picks == 2
    assert degraded.wins == 1
    assert degraded.losses == 1


def test_by_profile_quality_side_b():
    """When pick_side=B, quality should come from profile_quality_b."""
    picks = [_pick(match_id="m1", pick_side="B",
                   profile_quality_a="full", profile_quality_b="degraded")]
    outcomes = [_outcome(match_id="m1", result="win")]
    bd = summarize_joined_records(picks, outcomes)

    assert "degraded" in bd.by_profile_quality
    assert "full" not in bd.by_profile_quality


# ── 5. Breakdown by_final_status ─────────────────────────────────────────────

def test_by_final_status():
    picks = [
        _pick(match_id="m1", final_status="PICK_ALERT_SENT"),
        _pick(match_id="m2", final_status="PICK_DRY_RUN"),
        _pick(match_id="m3", final_status="PICK_ALERT_SENT"),
    ]
    outcomes = [
        _outcome(match_id="m1", result="win"),
        _outcome(match_id="m2", result="loss"),
        _outcome(match_id="m3", result="loss"),
    ]
    bd = summarize_joined_records(picks, outcomes)

    sent = bd.by_final_status["PICK_ALERT_SENT"]
    assert sent.total_picks == 2
    assert sent.wins == 1
    assert sent.losses == 1

    dry = bd.by_final_status["PICK_DRY_RUN"]
    assert dry.total_picks == 1
    assert dry.losses == 1


# ── 6. Breakdown by_is_dry_run ──────────────────────────────────────────────

def test_by_is_dry_run():
    picks = [
        _pick(match_id="m1", is_dry_run=False),
        _pick(match_id="m2", is_dry_run=True),
        _pick(match_id="m3", is_dry_run=False),
    ]
    outcomes = [
        _outcome(match_id="m1", result="win"),
        _outcome(match_id="m2", result="win", odds=2.00),
        _outcome(match_id="m3", result="loss"),
    ]
    bd = summarize_joined_records(picks, outcomes)

    live = bd.by_is_dry_run["live"]
    assert live.total_picks == 2
    assert live.wins == 1
    assert live.losses == 1

    dry = bd.by_is_dry_run["dry_run"]
    assert dry.total_picks == 1
    assert dry.wins == 1


# ── 7. ROI correct dans chaque bucket ───────────────────────────────────────

def test_roi_per_bucket():
    picks = [
        _pick(match_id="m1", odds=1.80, stake_units=1.0,
              profile_quality_a="full"),
        _pick(match_id="m2", odds=2.50, stake_units=1.0,
              profile_quality_a="degraded"),
    ]
    outcomes = [
        _outcome(match_id="m1", result="win", odds=1.80, stake_units=1.0),
        _outcome(match_id="m2", result="win", odds=2.50, stake_units=1.0),
    ]
    bd = summarize_joined_records(picks, outcomes)

    full = bd.by_profile_quality["full"]
    # ROI = 0.80 / 1.0 = 0.80
    assert full.roi == pytest.approx(0.80, abs=1e-4)

    degraded = bd.by_profile_quality["degraded"]
    # ROI = 1.50 / 1.0 = 1.50
    assert degraded.roi == pytest.approx(1.50, abs=1e-4)


def test_roi_negative_bucket():
    picks = [_pick(match_id="m1", odds=1.80, stake_units=1.0)]
    outcomes = [_outcome(match_id="m1", result="loss", odds=1.80, stake_units=1.0)]
    bd = summarize_joined_records(picks, outcomes)
    b = bd.by_profile_quality["full"]
    assert b.roi == pytest.approx(-1.0, abs=1e-4)


# ── 8. total_picks vs settled_picks with unsettled picks ─────────────────────

def test_unsettled_picks():
    picks = [
        _pick(match_id="m1"),
        _pick(match_id="m2"),
        _pick(match_id="m3"),
    ]
    outcomes = [
        _outcome(match_id="m1", result="win"),
        # m2 and m3 have no outcome
    ]
    bd = summarize_joined_records(picks, outcomes)

    b = bd.by_profile_quality["full"]
    assert b.total_picks == 3
    assert b.settled_picks == 1
    assert b.wins == 1
    assert b.losses == 0


def test_unsettled_picks_roi_only_from_settled():
    """ROI should only reflect settled outcomes, not total picks."""
    picks = [
        _pick(match_id="m1", odds=2.00, stake_units=1.0),
        _pick(match_id="m2", odds=1.80, stake_units=1.0),  # unsettled
    ]
    outcomes = [
        _outcome(match_id="m1", result="win", odds=2.00, stake_units=1.0),
    ]
    bd = summarize_joined_records(picks, outcomes)
    b = bd.by_profile_quality["full"]
    assert b.total_picks == 2
    assert b.settled_picks == 1
    # total_stake_units only from settled outcome
    assert b.total_stake_units == pytest.approx(1.0)
    assert b.roi == pytest.approx(1.0, abs=1e-4)


# ── 9. load_and_summarize_breakdown ──────────────────────────────────────────

def test_load_and_summarize_breakdown(tmp_path):
    picks_dir = str(tmp_path / "picks")
    outcomes_dir = str(tmp_path / "outcomes")
    os.makedirs(picks_dir)
    os.makedirs(outcomes_dir)

    with open(os.path.join(picks_dir, "2026-03-24.jsonl"), "w") as f:
        f.write(json.dumps(_pick(match_id="m1")) + "\n")

    with open(os.path.join(outcomes_dir, "2026-03-24.jsonl"), "w") as f:
        f.write(json.dumps(_outcome(match_id="m1", result="win")) + "\n")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir):
        bd = load_and_summarize_breakdown("2026-03-24")

    assert bd.by_profile_quality["full"].wins == 1
    assert bd.by_final_status["PICK_ALERT_SENT"].wins == 1
    assert bd.by_is_dry_run["live"].wins == 1


def test_load_and_summarize_breakdown_empty(tmp_path):
    with patch("tennis_model.tracking.pick_store._PICKS_DIR", str(tmp_path)), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", str(tmp_path)):
        bd = load_and_summarize_breakdown("2099-01-01")
    assert bd.by_profile_quality == {}


# ── 10. Win-rate guard on empty bucket ───────────────────────────────────────

def test_win_rate_zero_settled():
    """Picks with no outcomes → win_rate = 0.0, not ZeroDivisionError."""
    picks = [_pick(match_id="m1")]
    bd = summarize_joined_records(picks, [])
    b = bd.by_profile_quality["full"]
    assert b.win_rate == 0.0
    assert b.roi == 0.0
