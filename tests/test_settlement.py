"""
tests/test_settlement.py
=========================
Tests for tracking/settlement.py — simple pick settlement (Step 2 post-P6).

Coverage:
  1. compute_profit_units win
  2. compute_profit_units loss
  3. settle_pick_record win
  4. settle_pick_record loss
  5. save_outcome_record creates readable JSONL
  6. load_outcome_records round-trip
  7. Non-blocking on disk error
  8. Compat with PickRecord from pick_store
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch

from tennis_model.tracking.pick_store import PickRecord
from tennis_model.tracking.settlement import (
    OutcomeRecord,
    compute_profit_units,
    settle_pick_record,
    save_outcome_record,
    load_outcome_records,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_pick(**overrides) -> PickRecord:
    """Build a minimal PickRecord with sensible defaults."""
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
    return PickRecord(**defaults)


# ── 1. compute_profit_units win ──────────────────────────────────────────────

def test_compute_profit_units_win():
    profit = compute_profit_units(odds=1.80, stake_units=1.0, result="win")
    assert profit == pytest.approx(0.80, abs=1e-4)


def test_compute_profit_units_win_higher_odds():
    profit = compute_profit_units(odds=2.50, stake_units=2.0, result="win")
    assert profit == pytest.approx(3.0, abs=1e-4)


# ── 2. compute_profit_units loss ─────────────────────────────────────────────

def test_compute_profit_units_loss():
    profit = compute_profit_units(odds=1.80, stake_units=1.0, result="loss")
    assert profit == pytest.approx(-1.0, abs=1e-4)


def test_compute_profit_units_loss_fractional_stake():
    profit = compute_profit_units(odds=2.10, stake_units=0.5, result="loss")
    assert profit == pytest.approx(-0.5, abs=1e-4)


# ── 3. settle_pick_record win ────────────────────────────────────────────────

def test_settle_pick_record_win():
    pick = _make_pick(pick_side="A", odds=1.80, stake_units=1.0)
    outcome = settle_pick_record(pick, winner="A")

    assert outcome.result == "win"
    assert outcome.winner == "A"
    assert outcome.profit_units == pytest.approx(0.80, abs=1e-4)
    assert outcome.match_id == pick.match_id
    assert outcome.date == pick.date
    assert outcome.player_a == pick.player_a
    assert outcome.player_b == pick.player_b
    assert outcome.pick_side == "A"
    assert outcome.odds == 1.80
    assert outcome.stake_units == 1.0


# ── 4. settle_pick_record loss ───────────────────────────────────────────────

def test_settle_pick_record_loss():
    pick = _make_pick(pick_side="A", odds=1.80, stake_units=1.0)
    outcome = settle_pick_record(pick, winner="B")

    assert outcome.result == "loss"
    assert outcome.winner == "B"
    assert outcome.profit_units == pytest.approx(-1.0, abs=1e-4)


def test_settle_pick_record_side_b_win():
    """Pick on side B, winner is B → win."""
    pick = _make_pick(pick_side="B", odds=2.10, stake_units=0.5)
    outcome = settle_pick_record(pick, winner="B")

    assert outcome.result == "win"
    assert outcome.profit_units == pytest.approx(0.55, abs=1e-4)


def test_settle_pick_record_side_b_loss():
    """Pick on side B, winner is A → loss."""
    pick = _make_pick(pick_side="B", odds=2.10, stake_units=0.5)
    outcome = settle_pick_record(pick, winner="A")

    assert outcome.result == "loss"
    assert outcome.profit_units == pytest.approx(-0.5, abs=1e-4)


# ── 4b. Invalid winner ──────────────────────────────────────────────────────

def test_settle_pick_record_invalid_winner():
    pick = _make_pick()
    with pytest.raises(ValueError, match="must be 'A' or 'B'"):
        settle_pick_record(pick, winner="X")


def test_settle_pick_record_lowercase_winner():
    """Winner arg is case-insensitive."""
    pick = _make_pick(pick_side="A", odds=1.80, stake_units=1.0)
    outcome = settle_pick_record(pick, winner="a")
    assert outcome.result == "win"
    assert outcome.winner == "A"


# ── 5. save_outcome_record creates readable JSONL ───────────────────────────

def test_save_outcome_record_creates_jsonl(tmp_path):
    outcome = OutcomeRecord(
        date="2026-03-24",
        match_id="2026-03-24_sinner_alcaraz",
        player_a="J. Sinner",
        player_b="C. Alcaraz",
        pick_side="A",
        winner="A",
        result="win",
        odds=1.80,
        stake_units=1.0,
        profit_units=0.80,
    )

    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", str(tmp_path)):
        save_outcome_record(outcome)

    path = os.path.join(str(tmp_path), "2026-03-24.jsonl")
    assert os.path.isfile(path)

    with open(path) as f:
        data = json.loads(f.readline())
    assert data["match_id"] == "2026-03-24_sinner_alcaraz"
    assert data["result"] == "win"
    assert data["profit_units"] == 0.80
    assert data["settled_at"]  # should be auto-filled


# ── 6. load_outcome_records round-trip ───────────────────────────────────────

def test_load_outcome_records_roundtrip(tmp_path):
    pick = _make_pick()
    outcome = settle_pick_record(pick, winner="A")

    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", str(tmp_path)):
        save_outcome_record(outcome)
        loaded = load_outcome_records("2026-03-24")

    assert len(loaded) == 1
    r = loaded[0]
    assert r["result"] == "win"
    assert r["profit_units"] == pytest.approx(0.80, abs=1e-4)
    assert r["winner"] == "A"
    assert r["settled_at"]


def test_load_outcome_records_multiple(tmp_path):
    pick_a = _make_pick(match_id="m1", pick_side="A", odds=1.80, stake_units=1.0)
    pick_b = _make_pick(match_id="m2", pick_side="B", odds=2.50, stake_units=0.5)

    o1 = settle_pick_record(pick_a, winner="A")
    o2 = settle_pick_record(pick_b, winner="A")

    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", str(tmp_path)):
        save_outcome_record(o1)
        save_outcome_record(o2)
        loaded = load_outcome_records("2026-03-24")

    assert len(loaded) == 2
    assert loaded[0]["result"] == "win"
    assert loaded[1]["result"] == "loss"


def test_load_outcome_records_missing_date(tmp_path):
    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", str(tmp_path)):
        records = load_outcome_records("2099-01-01")
    assert records == []


# ── 7. Non-blocking on disk error ────────────────────────────────────────────

def test_save_outcome_record_nonblocking_on_disk_error():
    outcome = OutcomeRecord(
        date="2026-03-24",
        match_id="test",
        player_a="A",
        player_b="B",
        pick_side="A",
        winner="B",
        result="loss",
        odds=1.80,
        stake_units=1.0,
        profit_units=-1.0,
    )

    with patch("tennis_model.tracking.settlement.append_jsonl",
               side_effect=OSError("disk full")):
        # Must NOT raise
        save_outcome_record(outcome)


# ── 8. Full flow: PickRecord → settle → persist → load ──────────────────────

def test_full_settlement_flow(tmp_path):
    """End-to-end: build PickRecord, settle it, save outcome, reload."""
    pick = _make_pick(
        match_id="2026-03-24_djokovic_nadal",
        player_a="N. Djokovic",
        player_b="R. Nadal",
        pick_side="B",
        odds=2.40,
        stake_units=0.75,
    )

    outcome = settle_pick_record(pick, winner="B")

    assert outcome.result == "win"
    assert outcome.profit_units == pytest.approx(1.05, abs=1e-4)
    assert outcome.player_a == "N. Djokovic"
    assert outcome.player_b == "R. Nadal"

    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", str(tmp_path)):
        save_outcome_record(outcome)
        loaded = load_outcome_records("2026-03-24")

    assert len(loaded) == 1
    r = loaded[0]
    assert r["match_id"] == "2026-03-24_djokovic_nadal"
    assert r["result"] == "win"
    assert r["profit_units"] == pytest.approx(1.05, abs=1e-4)
    assert r["pick_side"] == "B"
    assert r["winner"] == "B"
