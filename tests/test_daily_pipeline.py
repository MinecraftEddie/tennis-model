"""
tests/test_daily_pipeline.py
==============================
Tests for scripts/daily_pipeline.py — daily automation orchestrator.

Coverage:
  1. main() runs end-to-end with scan skipped (no network)
  2. step_settle returns correct count
  3. step_performance writes summary.json
  4. step_blocked_diagnostic does not crash on missing audit
  5. step_calibration_diagnostic does not crash on empty data
  6. log file created
  7. manual_results missing → handled safely
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, MagicMock

from tennis_model.tracking.pick_store import append_jsonl
from tennis_model.tracking.settlement import load_outcome_records


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_pick_dict(**overrides) -> dict:
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


def _setup_data(tmp_path, date_str):
    """Create picks, winners, and directory structure for testing."""
    # Picks
    picks_dir = os.path.join(str(tmp_path), "picks")
    os.makedirs(picks_dir, exist_ok=True)
    picks_path = os.path.join(picks_dir, f"{date_str}.jsonl")
    append_jsonl(picks_path, _make_pick_dict(match_id="m1", pick_side="A", odds=1.80))
    append_jsonl(picks_path, _make_pick_dict(match_id="m2", pick_side="B", odds=2.10))

    # Winners
    results_dir = os.path.join(str(tmp_path), "manual_results")
    os.makedirs(results_dir, exist_ok=True)
    with open(os.path.join(results_dir, f"{date_str}.json"), "w") as f:
        json.dump({"m1": "A", "m2": "A"}, f)

    # Outcomes, performance, logs dirs
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")
    perf_dir = os.path.join(str(tmp_path), "performance")
    logs_dir = os.path.join(str(tmp_path), "logs")
    audits_dir = os.path.join(str(tmp_path), "audits")
    for d in [outcomes_dir, perf_dir, logs_dir, audits_dir]:
        os.makedirs(d, exist_ok=True)

    return {
        "picks_dir": picks_dir,
        "outcomes_dir": outcomes_dir,
        "results_dir": results_dir,
        "perf_dir": perf_dir,
        "logs_dir": logs_dir,
        "audits_dir": audits_dir,
    }


# ── 1. End-to-end with scan skipped ─────────────────────────────────────────

def test_main_end_to_end(tmp_path):
    date_str = "2026-03-24"
    dirs = _setup_data(tmp_path, date_str)

    # Import after sys.path is set
    from scripts.daily_pipeline import main

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", dirs["picks_dir"]), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", dirs["outcomes_dir"]), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", dirs["results_dir"]), \
         patch("scripts.daily_pipeline._PERF_DIR", dirs["perf_dir"]), \
         patch("scripts.daily_pipeline._LOGS_DIR", dirs["logs_dir"]), \
         patch("tennis_model.tracking.blocked_diagnostic._AUDITS_DIR", dirs["audits_dir"]):
        result = main(date_str=date_str, skip_scan=True)

    assert result["date"] == date_str
    assert result["scan_ok"] is True
    assert result["settled_count"] == 2
    assert "settle" in result["steps_completed"]
    assert "performance" in result["steps_completed"]
    assert "blocked_diagnostic" in result["steps_completed"]
    assert "calibration_diagnostic" in result["steps_completed"]


# ── 2. step_settle returns correct count ─────────────────────────────────────

def test_step_settle(tmp_path):
    date_str = "2026-03-24"
    dirs = _setup_data(tmp_path, date_str)

    import logging
    log = logging.getLogger("test_settle")

    from scripts.daily_pipeline import step_settle

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", dirs["picks_dir"]), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", dirs["outcomes_dir"]), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", dirs["results_dir"]):
        count = step_settle(log, date_str)

    assert count == 2


# ── 3. step_performance writes summary.json ─────────────────────────────────

def test_step_performance_writes_summary(tmp_path):
    date_str = "2026-03-24"
    dirs = _setup_data(tmp_path, date_str)

    import logging
    log = logging.getLogger("test_perf")

    from scripts.daily_pipeline import step_settle, step_performance

    # Settle first so there are outcomes
    with patch("tennis_model.tracking.pick_store._PICKS_DIR", dirs["picks_dir"]), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", dirs["outcomes_dir"]), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", dirs["results_dir"]):
        step_settle(log, date_str)

    with patch("tennis_model.tracking.settlement._OUTCOMES_DIR", dirs["outcomes_dir"]), \
         patch("scripts.daily_pipeline._PERF_DIR", dirs["perf_dir"]):
        step_performance(log, date_str)

    summary_path = os.path.join(dirs["perf_dir"], "summary.json")
    assert os.path.isfile(summary_path)
    with open(summary_path) as f:
        data = json.load(f)
    assert data["settled_picks"] == 2
    assert data["wins"] == 1
    assert data["losses"] == 1


# ── 4. Blocked diagnostic does not crash on missing audit ────────────────────

def test_step_blocked_diagnostic_missing_audit(tmp_path):
    import logging
    log = logging.getLogger("test_blocked")

    from scripts.daily_pipeline import step_blocked_diagnostic

    with patch("tennis_model.tracking.blocked_diagnostic._AUDITS_DIR", str(tmp_path)):
        # Should not raise
        step_blocked_diagnostic(log, "2026-03-24")


# ── 5. Calibration diagnostic does not crash on empty data ──────────────────

def test_step_calibration_diagnostic_empty(tmp_path):
    import logging
    log = logging.getLogger("test_cal")

    from scripts.daily_pipeline import step_calibration_diagnostic

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", str(tmp_path)), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", str(tmp_path)):
        # Should not raise
        step_calibration_diagnostic(log, "2026-03-24")


# ── 6. Log file created ────────────────────────────────────────────────────

def test_log_file_created(tmp_path):
    date_str = "2026-03-24"
    dirs = _setup_data(tmp_path, date_str)

    from scripts.daily_pipeline import main

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", dirs["picks_dir"]), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", dirs["outcomes_dir"]), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", dirs["results_dir"]), \
         patch("scripts.daily_pipeline._PERF_DIR", dirs["perf_dir"]), \
         patch("scripts.daily_pipeline._LOGS_DIR", dirs["logs_dir"]), \
         patch("tennis_model.tracking.blocked_diagnostic._AUDITS_DIR", dirs["audits_dir"]):
        main(date_str=date_str, skip_scan=True)

    log_path = os.path.join(dirs["logs_dir"], "daily_pipeline.log")
    assert os.path.isfile(log_path)
    with open(log_path, encoding="utf-8") as f:
        content = f.read()
    assert "Daily pipeline started" in content
    assert "Daily pipeline finished" in content


# ── 7. Missing manual_results handled safely ────────────────────────────────

def test_main_no_winners_no_crash(tmp_path):
    date_str = "2026-03-24"
    # Only create picks, no winners file
    picks_dir = os.path.join(str(tmp_path), "picks")
    os.makedirs(picks_dir, exist_ok=True)
    picks_path = os.path.join(picks_dir, f"{date_str}.jsonl")
    append_jsonl(picks_path, _make_pick_dict(match_id="m1"))

    empty_results = os.path.join(str(tmp_path), "manual_results")
    outcomes_dir = os.path.join(str(tmp_path), "outcomes")
    perf_dir = os.path.join(str(tmp_path), "performance")
    logs_dir = os.path.join(str(tmp_path), "logs")
    audits_dir = os.path.join(str(tmp_path), "audits")

    from scripts.daily_pipeline import main

    with patch("tennis_model.tracking.pick_store._PICKS_DIR", picks_dir), \
         patch("tennis_model.tracking.settlement._OUTCOMES_DIR", outcomes_dir), \
         patch("tennis_model.tracking.auto_settlement._MANUAL_RESULTS_DIR", empty_results), \
         patch("scripts.daily_pipeline._PERF_DIR", perf_dir), \
         patch("scripts.daily_pipeline._LOGS_DIR", logs_dir), \
         patch("tennis_model.tracking.blocked_diagnostic._AUDITS_DIR", audits_dir):
        result = main(date_str=date_str, skip_scan=True)

    assert result["settled_count"] == 0
    assert len(result["steps_completed"]) == 4  # settle, performance, blocked, calibration
