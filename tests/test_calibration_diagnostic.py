"""
tests/test_calibration_diagnostic.py
======================================
Tests for tracking/calibration_diagnostic.py — Step 5 post-P6.

Coverage:
  1. Diagnostic vide
  2. Breakdown odds range correct
  3. Breakdown stake range correct
  4. Warnings absents quand trop peu d'échantillons
  5. Warning odds 3.00+ fortement négatif
  6. Warning degraded fortement négatif
  7. Warning dry_run vs live si différence marquée
  8. build_calibration_diagnostic bout en bout
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch

from tennis_model.tracking.calibration_diagnostic import (
    CalibrationDiagnostic,
    CalibrationBucket,
    summarize_by_odds_range,
    summarize_by_stake_range,
    build_calibration_diagnostic,
    format_calibration_diagnostic,
    _generate_warnings,
    _MIN_SAMPLE,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pick(match_id="m1", odds=1.80, stake_units=1.0,
          profile_quality_a="full", profile_quality_b="full",
          pick_side="A", final_status="PICK_ALERT_SENT", is_dry_run=False):
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
        "reason_codes": [],
        "confidence": "HIGH",
        "ev": 0.10,
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


def _outcome_index(outcomes):
    """Build match_id → outcome dict."""
    return {o["match_id"]: o for o in outcomes}


def _batch(n, result="loss", odds=3.50, stake_units=1.0, **pick_kw):
    """Generate n pick/outcome pairs."""
    picks, outcomes = [], []
    for i in range(n):
        mid = f"m{i}"
        picks.append(_pick(match_id=mid, odds=odds, stake_units=stake_units, **pick_kw))
        outcomes.append(_outcome(match_id=mid, result=result, odds=odds, stake_units=stake_units))
    return picks, outcomes


# ── 1. Diagnostic vide ──────────────────────────────────────────────────────

def test_empty_diagnostic():
    picks, outcomes = [], []
    idx = _outcome_index(outcomes)

    by_odds = summarize_by_odds_range(picks, idx)
    by_stake = summarize_by_stake_range(picks, idx)

    assert by_odds == {}
    assert by_stake == {}


# ── 2. Breakdown odds range correct ─────────────────────────────────────────

def test_odds_range_bucketing():
    picks = [
        _pick(match_id="a", odds=1.30),
        _pick(match_id="b", odds=1.75),
        _pick(match_id="c", odds=2.40),
        _pick(match_id="d", odds=3.50),
    ]
    outcomes = [
        _outcome(match_id="a", result="win",  odds=1.30),
        _outcome(match_id="b", result="loss", odds=1.75),
        _outcome(match_id="c", result="win",  odds=2.40),
        _outcome(match_id="d", result="loss", odds=3.50),
    ]
    idx = _outcome_index(outcomes)
    by_odds = summarize_by_odds_range(picks, idx)

    assert by_odds["<1.50"].total_picks == 1
    assert by_odds["<1.50"].wins == 1
    assert by_odds["1.50-1.99"].total_picks == 1
    assert by_odds["1.50-1.99"].losses == 1
    assert by_odds["2.00-2.99"].total_picks == 1
    assert by_odds["2.00-2.99"].wins == 1
    assert by_odds["3.00+"].total_picks == 1
    assert by_odds["3.00+"].losses == 1


def test_odds_range_boundary_1_50():
    """1.50 should fall into '1.50-1.99', not '<1.50'."""
    picks = [_pick(match_id="a", odds=1.50)]
    outcomes = [_outcome(match_id="a", odds=1.50)]
    idx = _outcome_index(outcomes)
    by_odds = summarize_by_odds_range(picks, idx)

    assert "<1.50" not in by_odds
    assert "1.50-1.99" in by_odds


def test_odds_range_boundary_3_00():
    """3.00 should fall into '3.00+'."""
    picks = [_pick(match_id="a", odds=3.00)]
    outcomes = [_outcome(match_id="a", odds=3.00)]
    idx = _outcome_index(outcomes)
    by_odds = summarize_by_odds_range(picks, idx)

    assert "3.00+" in by_odds
    assert "2.00-2.99" not in by_odds


# ── 3. Breakdown stake range correct ────────────────────────────────────────

def test_stake_range_bucketing():
    picks = [
        _pick(match_id="a", stake_units=0.25),
        _pick(match_id="b", stake_units=0.75),
        _pick(match_id="c", stake_units=1.50),
    ]
    outcomes = [
        _outcome(match_id="a", result="win",  stake_units=0.25, odds=1.80),
        _outcome(match_id="b", result="loss", stake_units=0.75, odds=1.80),
        _outcome(match_id="c", result="win",  stake_units=1.50, odds=2.00),
    ]
    idx = _outcome_index(outcomes)
    by_stake = summarize_by_stake_range(picks, idx)

    assert by_stake["<0.50"].total_picks == 1
    assert by_stake["0.50-0.99"].total_picks == 1
    assert by_stake["1.00+"].total_picks == 1


def test_stake_range_boundary_0_50():
    """0.50 → '0.50-0.99'."""
    picks = [_pick(match_id="a", stake_units=0.50)]
    idx = _outcome_index([_outcome(match_id="a")])
    by_stake = summarize_by_stake_range(picks, idx)

    assert "0.50-0.99" in by_stake
    assert "<0.50" not in by_stake


# ── 4. Warnings absents quand trop peu d'échantillons ───────────────────────

def test_no_warnings_small_sample():
    """With fewer than _MIN_SAMPLE settled picks, no warnings should fire."""
    picks = [_pick(match_id=f"m{i}", odds=3.50,
                   profile_quality_a="degraded") for i in range(5)]
    outcomes = [_outcome(match_id=f"m{i}", result="loss", odds=3.50) for i in range(5)]
    idx = _outcome_index(outcomes)

    from tennis_model.tracking.performance_breakdown import summarize_joined_records
    bd = summarize_joined_records(picks, outcomes)

    diag = CalibrationDiagnostic(
        by_profile_quality=bd.by_profile_quality,
        by_is_dry_run=bd.by_is_dry_run,
        by_odds_range=summarize_by_odds_range(picks, idx),
        by_stake_range=summarize_by_stake_range(picks, idx),
    )
    diag.warnings = _generate_warnings(diag)
    assert diag.warnings == []


# ── 5. Warning odds 3.00+ fortement négatif ─────────────────────────────────

def test_warning_high_odds_negative():
    picks, outcomes = _batch(_MIN_SAMPLE, result="loss", odds=3.50)
    idx = _outcome_index(outcomes)

    from tennis_model.tracking.performance_breakdown import summarize_joined_records
    bd = summarize_joined_records(picks, outcomes)

    diag = CalibrationDiagnostic(
        by_profile_quality=bd.by_profile_quality,
        by_is_dry_run=bd.by_is_dry_run,
        by_odds_range=summarize_by_odds_range(picks, idx),
        by_stake_range=summarize_by_stake_range(picks, idx),
    )
    diag.warnings = _generate_warnings(diag)
    assert any("3.00+" in w for w in diag.warnings)


def test_no_warning_high_odds_positive():
    picks, outcomes = _batch(_MIN_SAMPLE, result="win", odds=3.50)
    idx = _outcome_index(outcomes)

    from tennis_model.tracking.performance_breakdown import summarize_joined_records
    bd = summarize_joined_records(picks, outcomes)

    diag = CalibrationDiagnostic(
        by_profile_quality=bd.by_profile_quality,
        by_is_dry_run=bd.by_is_dry_run,
        by_odds_range=summarize_by_odds_range(picks, idx),
        by_stake_range=summarize_by_stake_range(picks, idx),
    )
    diag.warnings = _generate_warnings(diag)
    assert not any("3.00+" in w for w in diag.warnings)


# ── 6. Warning degraded fortement négatif ────────────────────────────────────

def test_warning_degraded_negative():
    picks, outcomes = _batch(_MIN_SAMPLE, result="loss", odds=1.80,
                             profile_quality_a="degraded")
    idx = _outcome_index(outcomes)

    from tennis_model.tracking.performance_breakdown import summarize_joined_records
    bd = summarize_joined_records(picks, outcomes)

    diag = CalibrationDiagnostic(
        by_profile_quality=bd.by_profile_quality,
        by_is_dry_run=bd.by_is_dry_run,
        by_odds_range=summarize_by_odds_range(picks, idx),
        by_stake_range=summarize_by_stake_range(picks, idx),
    )
    diag.warnings = _generate_warnings(diag)
    assert any("DEGRADED" in w for w in diag.warnings)


def test_no_warning_degraded_positive():
    picks, outcomes = _batch(_MIN_SAMPLE, result="win", odds=1.80,
                             profile_quality_a="degraded")
    idx = _outcome_index(outcomes)

    from tennis_model.tracking.performance_breakdown import summarize_joined_records
    bd = summarize_joined_records(picks, outcomes)

    diag = CalibrationDiagnostic(
        by_profile_quality=bd.by_profile_quality,
        by_is_dry_run=bd.by_is_dry_run,
        by_odds_range=summarize_by_odds_range(picks, idx),
        by_stake_range=summarize_by_stake_range(picks, idx),
    )
    diag.warnings = _generate_warnings(diag)
    assert not any("DEGRADED" in w for w in diag.warnings)


# ── 7. Warning dry_run vs live si différence marquée ─────────────────────────

def test_warning_dry_live_gap():
    # Live: all wins → positive ROI
    live_picks, live_outcomes = _batch(_MIN_SAMPLE, result="win", odds=1.80,
                                       is_dry_run=False)
    # Dry: all losses → ROI = -1.0
    dry_picks, dry_outcomes = _batch(_MIN_SAMPLE, result="loss", odds=1.80,
                                     is_dry_run=True)
    # Offset dry match_ids to avoid collision
    for i, (p, o) in enumerate(zip(dry_picks, dry_outcomes)):
        p["match_id"] = f"d{i}"
        o["match_id"] = f"d{i}"

    all_picks = live_picks + dry_picks
    all_outcomes = live_outcomes + dry_outcomes

    from tennis_model.tracking.performance_breakdown import summarize_joined_records
    bd = summarize_joined_records(all_picks, all_outcomes)

    idx = _outcome_index(all_outcomes)
    diag = CalibrationDiagnostic(
        by_profile_quality=bd.by_profile_quality,
        by_is_dry_run=bd.by_is_dry_run,
        by_odds_range=summarize_by_odds_range(all_picks, idx),
        by_stake_range=summarize_by_stake_range(all_picks, idx),
    )
    diag.warnings = _generate_warnings(diag)
    assert any("Dry-run" in w for w in diag.warnings)


def test_no_warning_dry_live_similar():
    live_picks, live_outcomes = _batch(_MIN_SAMPLE, result="win", odds=1.80,
                                       is_dry_run=False)
    dry_picks, dry_outcomes = _batch(_MIN_SAMPLE, result="win", odds=1.80,
                                     is_dry_run=True)
    for i, (p, o) in enumerate(zip(dry_picks, dry_outcomes)):
        p["match_id"] = f"d{i}"
        o["match_id"] = f"d{i}"

    all_picks = live_picks + dry_picks
    all_outcomes = live_outcomes + dry_outcomes

    from tennis_model.tracking.performance_breakdown import summarize_joined_records
    bd = summarize_joined_records(all_picks, all_outcomes)

    idx = _outcome_index(all_outcomes)
    diag = CalibrationDiagnostic(
        by_profile_quality=bd.by_profile_quality,
        by_is_dry_run=bd.by_is_dry_run,
        by_odds_range=summarize_by_odds_range(all_picks, idx),
        by_stake_range=summarize_by_stake_range(all_picks, idx),
    )
    diag.warnings = _generate_warnings(diag)
    assert not any("Dry-run" in w for w in diag.warnings)


# ── 8. build_calibration_diagnostic bout en bout ─────────────────────────────

def test_build_calibration_diagnostic_e2e(tmp_path):
    picks_dir = str(tmp_path / "picks")
    outcomes_dir = str(tmp_path / "outcomes")
    os.makedirs(picks_dir)
    os.makedirs(outcomes_dir)

    picks = [
        _pick(match_id="m1", odds=1.80, stake_units=1.0),
        _pick(match_id="m2", odds=3.20, stake_units=0.5),
    ]
    outcomes = [
        _outcome(match_id="m1", result="win",  odds=1.80, stake_units=1.0),
        _outcome(match_id="m2", result="loss", odds=3.20, stake_units=0.5),
    ]

    with open(os.path.join(picks_dir, "2026-03-24.jsonl"), "w") as f:
        for p in picks:
            f.write(json.dumps(p) + "\n")
    with open(os.path.join(outcomes_dir, "2026-03-24.jsonl"), "w") as f:
        for o in outcomes:
            f.write(json.dumps(o) + "\n")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir):
        diag = build_calibration_diagnostic("2026-03-24")

    # Global
    assert diag.global_summary.total_picks == 2
    assert diag.global_summary.settled_picks == 2
    assert diag.global_summary.wins == 1
    assert diag.global_summary.losses == 1

    # Odds ranges populated
    assert "1.50-1.99" in diag.by_odds_range
    assert "3.00+" in diag.by_odds_range

    # Stake ranges populated
    assert "1.00+" in diag.by_stake_range
    assert "0.50-0.99" in diag.by_stake_range or "<0.50" in diag.by_stake_range

    # Quality / status / dry_run inherited from breakdown
    assert "full" in diag.by_profile_quality
    assert "live" in diag.by_is_dry_run

    # Warnings list exists (may be empty with only 2 picks)
    assert isinstance(diag.warnings, list)


def test_build_calibration_diagnostic_empty(tmp_path):
    with patch("tennis_model.tracking.pick_store._PICKS_DIR", str(tmp_path)), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", str(tmp_path)):
        diag = build_calibration_diagnostic("2099-01-01")

    assert diag.global_summary.total_picks == 0
    assert diag.by_odds_range == {}
    assert diag.by_stake_range == {}
    assert diag.warnings == []


# ── 9. Warning on high stake negative ────────────────────────────────────────

def test_warning_high_stake_negative():
    picks, outcomes = _batch(_MIN_SAMPLE, result="loss", odds=1.80,
                             stake_units=1.50)
    idx = _outcome_index(outcomes)

    from tennis_model.tracking.performance_breakdown import summarize_joined_records
    bd = summarize_joined_records(picks, outcomes)

    diag = CalibrationDiagnostic(
        by_profile_quality=bd.by_profile_quality,
        by_is_dry_run=bd.by_is_dry_run,
        by_odds_range=summarize_by_odds_range(picks, idx),
        by_stake_range=summarize_by_stake_range(picks, idx),
    )
    diag.warnings = _generate_warnings(diag)
    assert any("1.00+" in w for w in diag.warnings)


# ── 10. Formatter tests ─────────────────────────────────────────────────

def test_format_empty_diagnostic():
    """format_calibration_diagnostic should not crash on an empty diagnostic."""
    diag = CalibrationDiagnostic()
    diag.warnings = []
    text = format_calibration_diagnostic(diag)
    assert "GLOBAL" in text
    assert "BY ODDS RANGE" in text
    assert "(none)" in text


def test_format_with_data(tmp_path):
    """Format output contains expected section headers and bucket data."""
    picks_dir = str(tmp_path / "picks")
    outcomes_dir = str(tmp_path / "outcomes")
    os.makedirs(picks_dir)
    os.makedirs(outcomes_dir)

    picks = [
        _pick(match_id="m1", odds=1.80, stake_units=1.0),
        _pick(match_id="m2", odds=3.20, stake_units=0.5),
    ]
    outcomes = [
        _outcome(match_id="m1", result="win",  odds=1.80, stake_units=1.0),
        _outcome(match_id="m2", result="loss", odds=3.20, stake_units=0.5),
    ]

    with open(os.path.join(picks_dir, "2026-03-24.jsonl"), "w") as f:
        for p in picks:
            f.write(json.dumps(p) + "\n")
    with open(os.path.join(outcomes_dir, "2026-03-24.jsonl"), "w") as f:
        for o in outcomes:
            f.write(json.dumps(o) + "\n")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir):
        diag = build_calibration_diagnostic("2026-03-24")

    text = format_calibration_diagnostic(diag)

    # All section headers present
    for header in ["GLOBAL", "BY PROFILE QUALITY", "BY FINAL STATUS",
                   "BY DRY RUN", "BY ODDS RANGE", "BY STAKE RANGE", "WARNINGS"]:
        assert header in text

    # Bucket metrics visible
    assert "picks=2" in text
    assert "settled=2" in text
    assert "W=1" in text


def test_format_with_warnings():
    """Warnings section shows actual warning text."""
    picks, outcomes = _batch(_MIN_SAMPLE, result="loss", odds=3.50)
    idx = _outcome_index(outcomes)

    from tennis_model.tracking.performance_breakdown import summarize_joined_records
    bd = summarize_joined_records(picks, outcomes)

    diag = CalibrationDiagnostic(
        by_profile_quality=bd.by_profile_quality,
        by_is_dry_run=bd.by_is_dry_run,
        by_odds_range=summarize_by_odds_range(picks, idx),
        by_stake_range=summarize_by_stake_range(picks, idx),
    )
    diag.warnings = _generate_warnings(diag)

    text = format_calibration_diagnostic(diag)
    assert "WARNINGS" in text
    assert "3.00+" in text
    assert "(none)" not in text
