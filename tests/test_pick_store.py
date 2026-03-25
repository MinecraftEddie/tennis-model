"""
tests/test_pick_store.py
=========================
Tests for tracking/pick_store.py — JSONL pick persistence (Step 1 post-P6).

Coverage:
  1. JSONL file created on first write
  2. Append multiple records to same file
  3. load_pick_records() round-trips correctly
  4. Non-blocking write on disk error (bad path)
  5. MatchRunResult PICK → produces PickRecord
  6. Dry-run correctly marked
  7. NO_PICK / WATCHLIST / BLOCKED not persisted
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock, patch

from tennis_model.tracking.pick_store import (
    PickRecord,
    append_jsonl,
    save_pick_record,
    load_pick_records,
    maybe_record_pick,
)


# ── Helpers to build minimal mock objects ────────────────────────────────────

def _make_pick(pick_player="Player A", player_a_name="Player A",
               player_b_name="Player B", market_odds_a=1.80,
               market_odds_b=2.10, confidence="HIGH", ev_a=0.12,
               ev_b=-0.05, stake_units=1.0):
    """Build a minimal MatchPick-like mock."""
    pa = MagicMock()
    pa.short_name = player_a_name
    pb = MagicMock()
    pb.short_name = player_b_name

    pick = MagicMock()
    pick.player_a = pa
    pick.player_b = pb
    pick.pick_player = pick_player
    pick.market_odds_a = market_odds_a
    pick.market_odds_b = market_odds_b
    pick.confidence = confidence
    pick.ev_a = ev_a
    pick.ev_b = ev_b
    pick.edge_a = 0.08
    pick.edge_b = -0.03
    pick.fair_odds_a = 1.65
    pick.fair_odds_b = 2.40
    pick.stake_units = stake_units

    def picked_side():
        if pick_player == player_a_name:
            return {
                "side": "A", "player": pa, "opponent": pb,
                "prob": 0.55, "market_odds": market_odds_a,
                "fair_odds": 1.65, "edge": 0.08, "ev": ev_a,
            }
        elif pick_player == player_b_name:
            return {
                "side": "B", "player": pb, "opponent": pa,
                "prob": 0.45, "market_odds": market_odds_b,
                "fair_odds": 2.40, "edge": -0.03, "ev": ev_b,
            }
        return None

    pick.picked_side = picked_side
    return pick


def _make_evaluator_decision(status_value="PICK", confidence=0.85):
    """Build a minimal EvaluatorDecision-like mock."""
    ed = MagicMock()
    ed.status = MagicMock()
    ed.status.value = status_value
    ed.confidence = confidence
    ed.reason_code = "PICK_APPROVED"
    return ed


def _make_risk_decision(stake_units=1.0, allowed=True):
    """Build a minimal RiskDecision-like mock."""
    rd = MagicMock()
    rd.stake_units = stake_units
    rd.allowed = allowed
    rd.stake_factor = 1.0
    return rd


def _make_match_run_result(final_status_value="PICK_ALERT_SENT",
                           pick=None, evaluator_decision=None,
                           risk_decision=None):
    """Build a minimal MatchRunResult-like mock."""
    result = MagicMock()
    result.match_id = "2026-03-24_player_a_player_b"
    result.player_a = "Player A"
    result.player_b = "Player B"
    result.profile_quality_a = "full"
    result.profile_quality_b = "full"
    result.reason_codes = ["PICK_APPROVED"]

    fs = MagicMock()
    fs.value = final_status_value
    result.final_status = fs

    result.pick = pick or _make_pick()
    result.evaluator_decision = evaluator_decision or _make_evaluator_decision()
    result.risk_decision = risk_decision or _make_risk_decision()

    return result


# ── 1. JSONL file created on first write ─────────────────────────────────────

def test_jsonl_file_created(tmp_path):
    path = str(tmp_path / "test.jsonl")
    assert not os.path.exists(path)

    append_jsonl(path, {"key": "value"})

    assert os.path.isfile(path)
    with open(path) as f:
        data = json.loads(f.readline())
    assert data == {"key": "value"}


# ── 2. Append multiple records ───────────────────────────────────────────────

def test_append_multiple_records(tmp_path):
    path = str(tmp_path / "multi.jsonl")

    append_jsonl(path, {"n": 1})
    append_jsonl(path, {"n": 2})
    append_jsonl(path, {"n": 3})

    with open(path) as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 3
    assert [r["n"] for r in lines] == [1, 2, 3]


# ── 3. load_pick_records round-trip ──────────────────────────────────────────

def test_load_pick_records_roundtrip(tmp_path):
    record = PickRecord(
        date="2026-03-24",
        match_id="2026-03-24_sinner_alcaraz",
        player_a="J. Sinner",
        player_b="C. Alcaraz",
        pick_side="A",
        odds=1.75,
        stake_units=1.0,
        profile_quality_a="full",
        profile_quality_b="full",
        evaluator_status="PICK",
        final_status="PICK_ALERT_SENT",
        reason_codes=["PICK_APPROVED"],
        confidence="HIGH",
        ev=0.12,
        is_dry_run=False,
    )

    # Point save to tmp dir
    with patch("tennis_model.tracking.pick_store._PICKS_DIR", str(tmp_path)):
        save_pick_record(record)
        loaded = load_pick_records("2026-03-24")

    assert len(loaded) == 1
    r = loaded[0]
    assert r["match_id"] == "2026-03-24_sinner_alcaraz"
    assert r["pick_side"] == "A"
    assert r["odds"] == 1.75
    assert r["confidence"] == "HIGH"
    assert r["is_dry_run"] is False
    assert r["created_at"]  # should be filled


# ── 4. Non-blocking on disk error ────────────────────────────────────────────

def test_save_pick_record_nonblocking_on_disk_error():
    """save_pick_record must not raise even if the path is invalid."""
    record = PickRecord(
        date="2026-03-24",
        match_id="test",
        player_a="A",
        player_b="B",
        pick_side="A",
        odds=1.80,
        stake_units=1.0,
        profile_quality_a="full",
        profile_quality_b="full",
        evaluator_status="PICK",
        final_status="PICK_ALERT_SENT",
    )

    # Force an OSError by patching append_jsonl to raise
    with patch("tennis_model.tracking.pick_store.append_jsonl",
               side_effect=OSError("disk full")):
        # Should NOT raise
        save_pick_record(record)


# ── 5. MatchRunResult PICK → produces PickRecord ────────────────────────────

def test_pick_result_produces_record(tmp_path):
    result = _make_match_run_result(final_status_value="PICK_ALERT_SENT")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", str(tmp_path)):
        record = maybe_record_pick(result)

    assert record is not None
    assert record.pick_side == "A"
    assert record.odds == 1.80
    assert record.stake_units == 1.0
    assert record.final_status == "PICK_ALERT_SENT"
    assert record.is_dry_run is False
    assert record.evaluator_status == "PICK"

    # Verify file written
    with patch("tennis_model.tracking.pick_store._PICKS_DIR", str(tmp_path)):
        loaded = load_pick_records(record.date)
    assert len(loaded) == 1


# ── 6. Dry-run correctly marked ─────────────────────────────────────────────

def test_dry_run_marked(tmp_path):
    result = _make_match_run_result(final_status_value="PICK_DRY_RUN")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", str(tmp_path)):
        record = maybe_record_pick(result)

    assert record is not None
    assert record.is_dry_run is True
    assert record.final_status == "PICK_DRY_RUN"


# ── 7. NO_PICK / WATCHLIST / BLOCKED not persisted ──────────────────────────

@pytest.mark.parametrize("status", [
    "NO_PICK",
    "WATCHLIST",
    "BLOCKED_MODEL",
    "BLOCKED_VALIDATION",
    "FAILED",
])
def test_non_pick_statuses_not_persisted(tmp_path, status):
    result = _make_match_run_result(final_status_value=status)

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", str(tmp_path)):
        record = maybe_record_pick(result)

    assert record is None

    # No file should be created
    files = os.listdir(str(tmp_path))
    assert len(files) == 0


# ── 8. PICK_SUPPRESSED is persisted ─────────────────────────────────────────

def test_pick_suppressed_persisted(tmp_path):
    result = _make_match_run_result(final_status_value="PICK_SUPPRESSED")

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", str(tmp_path)):
        record = maybe_record_pick(result)

    assert record is not None
    assert record.final_status == "PICK_SUPPRESSED"
    assert record.is_dry_run is False


# ── 9. maybe_record_pick non-blocking on exception ──────────────────────────

def test_maybe_record_pick_nonblocking_on_exception():
    """maybe_record_pick must swallow any exception."""
    result = MagicMock()
    # Force AttributeError by removing final_status
    del result.final_status

    # Should not raise
    record = maybe_record_pick(result)
    assert record is None


# ── 10. Confidence from evaluator decision preferred ─────────────────────────

def test_confidence_from_evaluator_decision(tmp_path):
    ed = _make_evaluator_decision(confidence=0.92)
    pick = _make_pick(confidence="MEDIUM")
    result = _make_match_run_result(
        final_status_value="PICK_ALERT_SENT",
        pick=pick,
        evaluator_decision=ed,
    )

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", str(tmp_path)):
        record = maybe_record_pick(result)

    # Evaluator confidence (numeric) takes priority
    assert record.confidence == "0.92"


# ── 11. Stake from risk_decision preferred ───────────────────────────────────

def test_stake_from_risk_decision(tmp_path):
    rd = _make_risk_decision(stake_units=0.5)
    pick = _make_pick(stake_units=1.0)
    result = _make_match_run_result(
        final_status_value="PICK_ALERT_SENT",
        pick=pick,
        risk_decision=rd,
    )

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", str(tmp_path)):
        record = maybe_record_pick(result)

    assert record.stake_units == 0.5


# ── 12. load_pick_records returns empty list for missing file ────────────────

def test_load_missing_date_returns_empty(tmp_path):
    with patch("tennis_model.tracking.pick_store._PICKS_DIR", str(tmp_path)):
        records = load_pick_records("2099-01-01")
    assert records == []
