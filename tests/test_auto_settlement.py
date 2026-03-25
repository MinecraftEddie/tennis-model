"""
tests/test_auto_settlement.py
==============================
Tests for tracking/auto_settlement.py — automatic settlement logic.

Coverage:
  1. No winners file → no crash, returns empty dict
  2. Malformed winners file → no crash
  3. Valid winners file → correct dict
  4. Invalid winner sides → skipped
  5. load_settled_match_ids returns correct set
  6. settle_unsettled_picks — winners present → outcomes created
  7. settle_unsettled_picks — already settled → no duplicate
  8. settle_unsettled_picks — no picks → returns 0
  9. settle_unsettled_picks — no winners file → returns 0
  10. settle_unsettled_picks — partial winners → only matching settled
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch

from tennis_model.tracking.auto_settlement import (
    load_manual_winners,
    load_settled_match_ids,
    settle_unsettled_picks,
)
from tennis_model.tracking.pick_store import PickRecord, append_jsonl
from tennis_model.tracking.settlement import (
    OutcomeRecord,
    save_outcome_record,
    load_outcome_records,
    settle_pick_record,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_pick_dict(**overrides) -> dict:
    """Build a minimal pick dict with sensible defaults."""
    defaults = dict(
        date="2026-03-24",
        match_id="2026-03-24_sinner_alcaraz",
        player_a="J. Sinner",
        player_b="C. Alcaraz",
        pick_side="A",
        odds=1.80,
        stake_units=1.0,
        profile_quality_a="full",
        profile_quality_b="full",
        evaluator_status="PICK",
        final_status="PICK_ALERT_SENT",
        reason_codes=["PICK_APPROVED"],
        confidence="HIGH",
        ev=0.12,
        is_dry_run=False,
        created_at="2026-03-24T10:00:00Z",
    )
    defaults.update(overrides)
    return defaults


def _write_winners(tmp_path, date_str, winners_dict):
    """Write a manual winners JSON file."""
    results_dir = os.path.join(str(tmp_path), "manual_results")
    os.makedirs(results_dir, exist_ok=True)
    path = os.path.join(results_dir, f"{date_str}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(winners_dict, f)
    return results_dir


def _write_picks(tmp_path, date_str, pick_dicts):
    """Write pick dicts to a JSONL file."""
    picks_dir = os.path.join(str(tmp_path), "picks")
    os.makedirs(picks_dir, exist_ok=True)
    path = os.path.join(picks_dir, f"{date_str}.jsonl")
    for p in pick_dicts:
        append_jsonl(path, p)
    return picks_dir


# ── 1. No winners file → no crash ───────────────────────────────────────────

def test_load_manual_winners_missing_file(tmp_path):
    with patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", str(tmp_path)):
        result = load_manual_winners("2026-03-24")
    assert result == {}


# ── 2. Malformed winners file → no crash ────────────────────────────────────

def test_load_manual_winners_malformed(tmp_path):
    path = os.path.join(str(tmp_path), "2026-03-24.json")
    with open(path, "w") as f:
        f.write("not json at all")
    with patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", str(tmp_path)):
        result = load_manual_winners("2026-03-24")
    assert result == {}


def test_load_manual_winners_not_object(tmp_path):
    path = os.path.join(str(tmp_path), "2026-03-24.json")
    with open(path, "w") as f:
        json.dump(["A", "B"], f)
    with patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", str(tmp_path)):
        result = load_manual_winners("2026-03-24")
    assert result == {}


# ── 3. Valid winners file → correct dict ────────────────────────────────────

def test_load_manual_winners_valid(tmp_path):
    winners = {"m1": "A", "m2": "B"}
    results_dir = _write_winners(tmp_path, "2026-03-24", winners)
    with patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", results_dir):
        result = load_manual_winners("2026-03-24")
    assert result == {"m1": "A", "m2": "B"}


def test_load_manual_winners_lowercase_normalised(tmp_path):
    winners = {"m1": "a", "m2": "b"}
    results_dir = _write_winners(tmp_path, "2026-03-24", winners)
    with patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", results_dir):
        result = load_manual_winners("2026-03-24")
    assert result == {"m1": "A", "m2": "B"}


# ── 4. Invalid winner sides → skipped ───────────────────────────────────────

def test_load_manual_winners_invalid_sides(tmp_path):
    winners = {"m1": "A", "m2": "X", "m3": "draw"}
    results_dir = _write_winners(tmp_path, "2026-03-24", winners)
    with patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", results_dir):
        result = load_manual_winners("2026-03-24")
    assert result == {"m1": "A"}


# ── 5. load_settled_match_ids ────────────────────────────────────────────────

def test_load_settled_match_ids_empty(tmp_path):
    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", str(tmp_path)):
        ids = load_settled_match_ids("2026-03-24")
    assert ids == set()


def test_load_settled_match_ids_with_outcomes(tmp_path):
    pick = PickRecord(
        date="2026-03-24", match_id="m1", player_a="A", player_b="B",
        pick_side="A", odds=1.80, stake_units=1.0, profile_quality_a="full",
        profile_quality_b="full", evaluator_status="PICK",
        final_status="PICK_ALERT_SENT",
    )
    outcome = settle_pick_record(pick, "A")
    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", str(tmp_path)):
        save_outcome_record(outcome)
        ids = load_settled_match_ids("2026-03-24")
    assert "m1" in ids


# ── 6. settle_unsettled_picks — winners present → outcomes created ───────────

def test_settle_creates_outcomes(tmp_path):
    date_str = "2026-03-24"
    picks_dir = _write_picks(tmp_path, date_str, [
        _make_pick_dict(match_id="m1", pick_side="A", odds=1.80),
        _make_pick_dict(match_id="m2", pick_side="B", odds=2.10),
    ])
    results_dir = _write_winners(tmp_path, date_str, {"m1": "A", "m2": "A"})
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", results_dir):
        count = settle_unsettled_picks(date_str)

    assert count == 2

    # Verify outcomes were written
    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir):
        outcomes = load_outcome_records(date_str)
    assert len(outcomes) == 2
    assert outcomes[0]["match_id"] == "m1"
    assert outcomes[0]["result"] == "win"
    assert outcomes[1]["match_id"] == "m2"
    assert outcomes[1]["result"] == "loss"


# ── 7. Already settled → no duplicate ────────────────────────────────────────

def test_settle_no_duplicate(tmp_path):
    date_str = "2026-03-24"
    picks_dir = _write_picks(tmp_path, date_str, [
        _make_pick_dict(match_id="m1", pick_side="A", odds=1.80),
    ])
    results_dir = _write_winners(tmp_path, date_str, {"m1": "A"})
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", results_dir):
        # First run
        count1 = settle_unsettled_picks(date_str)
        # Second run — should settle nothing
        count2 = settle_unsettled_picks(date_str)

    assert count1 == 1
    assert count2 == 0

    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir):
        outcomes = load_outcome_records(date_str)
    assert len(outcomes) == 1


# ── 8. No picks → returns 0 ─────────────────────────────────────────────────

def test_settle_no_picks(tmp_path):
    date_str = "2026-03-24"
    picks_dir = os.path.join(str(tmp_path), "picks")
    results_dir = _write_winners(tmp_path, date_str, {"m1": "A"})
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", results_dir):
        count = settle_unsettled_picks(date_str)

    assert count == 0


# ── 9. No winners file → returns 0 ──────────────────────────────────────────

def test_settle_no_winners_file(tmp_path):
    date_str = "2026-03-24"
    picks_dir = _write_picks(tmp_path, date_str, [
        _make_pick_dict(match_id="m1"),
    ])
    empty_results_dir = os.path.join(str(tmp_path), "manual_results")
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", empty_results_dir):
        count = settle_unsettled_picks(date_str)

    assert count == 0


# ── 10. Partial winners → only matching settled ─────────────────────────────

def test_settle_partial_winners(tmp_path):
    date_str = "2026-03-24"
    picks_dir = _write_picks(tmp_path, date_str, [
        _make_pick_dict(match_id="m1", pick_side="A", odds=1.80),
        _make_pick_dict(match_id="m2", pick_side="B", odds=2.10),
        _make_pick_dict(match_id="m3", pick_side="A", odds=1.50),
    ])
    # Only m1 and m3 have winners — m2 should be skipped
    results_dir = _write_winners(tmp_path, date_str, {"m1": "A", "m3": "B"})
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", results_dir):
        count = settle_unsettled_picks(date_str)

    assert count == 2

    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir):
        outcomes = load_outcome_records(date_str)
    settled_ids = {o["match_id"] for o in outcomes}
    assert settled_ids == {"m1", "m3"}
    # m1: pick A, winner A → win; m3: pick A, winner B → loss
    by_id = {o["match_id"]: o for o in outcomes}
    assert by_id["m1"]["result"] == "win"
    assert by_id["m3"]["result"] == "loss"
