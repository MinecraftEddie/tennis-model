"""
tests/test_p5_audit.py
========================
P5 tests — DailyAudit.record_match_result(), final_status_breakdown,
and counter unification (watchlist_count / evaluator_watchlist_count).

Coverage:
  A. record_match_result() increments final_status_breakdown
  B. record_match_result() delegates to record_evaluator_decision()
  C. record_match_result() delegates to record_alert_decision() for PICK path
  D. watchlist_count unified: driven by record_match_result(), not double-counted
  E. save_audit_json() includes final_status_breakdown
  F. populate_from_scan_results() watchlist fallback guard
  G. Non-regression: P3/P4 breakdown fields still populated correctly
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import pytest
import tempfile
from unittest.mock import MagicMock

from tennis_model.orchestration.audit import DailyAudit
from tennis_model.orchestration.match_runner import MatchFinalStatus, MatchRunResult
from tennis_model.evaluator.evaluator_decision import EvaluatorStatus
from tennis_model.orchestration.alert_status import AlertStatus
from tennis_model.quality.reason_codes import ReasonCode


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ed(status: EvaluatorStatus, rec_action: str = "send"):
    d = MagicMock()
    d.status = status
    d.recommended_action = rec_action
    d.reason_code = ReasonCode.PICK_APPROVED
    d.filter_reason = None
    d.confidence = 0.7
    d.message = None
    return d


def _make_ad(status: AlertStatus, stake_units: float = 1.0, stake_factor: float = 1.0):
    d = MagicMock()
    d.status = status
    d.stake_units = stake_units
    d.stake_factor = stake_factor
    return d


def _make_result(
    final_status: MatchFinalStatus,
    eval_status: EvaluatorStatus = EvaluatorStatus.PICK,
    alert_status: AlertStatus = AlertStatus.SENT,
    alert_decision=None,
    filter_reason: str = None,
) -> MatchRunResult:
    ed = _make_ed(eval_status)
    ad = alert_decision
    if ad is None and eval_status == EvaluatorStatus.PICK:
        ad = _make_ad(alert_status)
    return MatchRunResult(
        match_id="2026-03-23_a_b",
        player_a="A",
        player_b="B",
        profile_quality_a="full",
        profile_quality_b="full",
        evaluator_decision=ed,
        final_status=final_status,
        reason_codes=[ReasonCode.PICK_APPROVED],
        alert_decision=ad,
        filter_reason=filter_reason,
    )


# ── A. final_status_breakdown ─────────────────────────────────────────────────

def test_record_match_result_populates_breakdown():
    audit = DailyAudit()
    result = _make_result(MatchFinalStatus.PICK_ALERT_SENT)
    audit.record_match_result(result)
    assert audit.final_status_breakdown == {"PICK_ALERT_SENT": 1}


def test_record_match_result_multiple_statuses():
    audit = DailyAudit()
    for _ in range(3):
        audit.record_match_result(_make_result(MatchFinalStatus.PICK_ALERT_SENT))
    # NO_PICK: alert_decision is None (EV blocked before alert path)
    audit.record_match_result(_make_result(
        MatchFinalStatus.NO_PICK,
        eval_status=EvaluatorStatus.NO_PICK,
        alert_decision=None,
    ))
    audit.record_match_result(_make_result(
        MatchFinalStatus.WATCHLIST,
        eval_status=EvaluatorStatus.WATCHLIST,
        alert_decision=None,
    ))
    assert audit.final_status_breakdown["PICK_ALERT_SENT"] == 3
    assert audit.final_status_breakdown["NO_PICK"] == 1
    assert audit.final_status_breakdown["WATCHLIST"] == 1


# ── B. Delegates to record_evaluator_decision() ───────────────────────────────

def test_record_match_result_increments_evaluator_status_breakdown():
    audit = DailyAudit()
    result = _make_result(MatchFinalStatus.PICK_ALERT_SENT, eval_status=EvaluatorStatus.PICK)
    audit.record_match_result(result)
    assert audit.evaluator_status_breakdown.get("PICK", 0) == 1


def test_record_match_result_no_pick_increments_no_pick_count():
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.NO_PICK,
        eval_status=EvaluatorStatus.NO_PICK,
        alert_decision=None,  # alert not reached on NO_PICK path
    )
    audit.record_match_result(result)
    assert audit.no_pick_count == 1


def test_record_match_result_blocked_validation():
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.BLOCKED_VALIDATION,
        eval_status=EvaluatorStatus.BLOCKED_VALIDATION,
        alert_decision=None,
    )
    audit.record_match_result(result)
    assert audit.validation_block_count == 1
    assert audit.final_status_breakdown.get("BLOCKED_VALIDATION") == 1


def test_record_match_result_blocked_model():
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.BLOCKED_MODEL,
        eval_status=EvaluatorStatus.BLOCKED_MODEL,
        alert_decision=None,
    )
    audit.record_match_result(result)
    assert audit.model_block_count == 1


# ── C. Delegates to record_alert_decision() ───────────────────────────────────

def test_record_match_result_dry_run_increments_alert_dry_run():
    audit = DailyAudit()
    ad = _make_ad(AlertStatus.DRY_RUN, stake_units=None, stake_factor=1.0)
    # stake_factor in [0,1) and stake_units is None → no stake_reduced_count
    result = _make_result(MatchFinalStatus.PICK_DRY_RUN, alert_decision=ad)
    audit.record_match_result(result)
    assert audit.alerts_dry_run == 1
    assert audit.alert_status_breakdown.get("DRY_RUN") == 1


def test_record_match_result_stake_reduced():
    audit = DailyAudit()
    ad = _make_ad(AlertStatus.SENT, stake_units=0.5, stake_factor=0.5)
    result = _make_result(MatchFinalStatus.PICK_ALERT_SENT, alert_decision=ad)
    audit.record_match_result(result)
    assert audit.stake_reduced_count == 1


def test_record_match_result_no_alert_decision_skips_alert_record():
    """Non-PICK path: no alert_decision → record_alert_decision not called."""
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.WATCHLIST,
        eval_status=EvaluatorStatus.WATCHLIST,
        alert_decision=None,
    )
    audit.record_match_result(result)
    # No alert counters should be touched
    assert audit.alerts_dry_run == 0
    assert audit.alert_status_breakdown == {}


# ── D. watchlist_count unified ────────────────────────────────────────────────

def test_watchlist_count_driven_by_record_match_result():
    audit = DailyAudit()
    for _ in range(2):
        result = _make_result(
            MatchFinalStatus.WATCHLIST,
            eval_status=EvaluatorStatus.WATCHLIST,
            alert_decision=None,
        )
        audit.record_match_result(result)
    assert audit.watchlist_count == 2
    assert audit.evaluator_watchlist_count == 2


def test_watchlist_count_not_doubled_by_populate():
    """
    If record_match_result() already set watchlist_count, populate_from_scan_results()
    should NOT add to it again (guard: only if watchlist_count == 0).
    """
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.WATCHLIST,
        eval_status=EvaluatorStatus.WATCHLIST,
        alert_decision=None,
    )
    audit.record_match_result(result)
    assert audit.watchlist_count == 1

    # Now call populate with an alert that has rec_action=="watchlist"
    alerts = [{"pick": "A", "rec_action": "watchlist", "qualified_only": True}]
    audit.populate_from_scan_results([], alerts, [], [])
    # Should NOT increment again since watchlist_count is already 1
    assert audit.watchlist_count == 1


def test_watchlist_fallback_in_populate_when_no_record_called():
    """populate_from_scan_results() fallback: populates watchlist_count if 0."""
    audit = DailyAudit()
    alerts = [{"pick": "A", "rec_action": "watchlist", "qualified_only": True}]
    audit.populate_from_scan_results([], alerts, [], [])
    assert audit.watchlist_count == 1


# ── E. save_audit_json() includes final_status_breakdown ─────────────────────

def test_save_audit_json_includes_final_status_breakdown():
    audit = DailyAudit()
    audit.record_match_result(
        _make_result(MatchFinalStatus.PICK_ALERT_SENT)
    )
    audit.record_match_result(
        _make_result(
            MatchFinalStatus.NO_PICK,
            eval_status=EvaluatorStatus.NO_PICK,
            alert_decision=None,
        )
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        audit.save_audit_json(audits_dir=tmpdir)
        path = os.path.join(tmpdir, f"{audit.date}.json")
        with open(path) as f:
            data = json.load(f)
    assert "final_status_breakdown" in data
    assert data["final_status_breakdown"]["PICK_ALERT_SENT"] == 1
    assert data["final_status_breakdown"]["NO_PICK"] == 1


# ── F. Legacy compat: save_audit_json still includes watchlist_count ──────────

def test_save_audit_json_legacy_fields_preserved():
    audit = DailyAudit()
    audit.picks_generated = 5
    audit.alerts_sent = 3
    audit.watchlist_count = 1
    with tempfile.TemporaryDirectory() as tmpdir:
        audit.save_audit_json(audits_dir=tmpdir)
        path = os.path.join(tmpdir, f"{audit.date}.json")
        with open(path) as f:
            data = json.load(f)
    assert data["picks_generated"] == 5
    assert data["alerts_sent"] == 3
    assert data["watchlist_count"] == 1


# ── G. Non-regression: P3/P4 breakdown fields ────────────────────────────────

def test_p4_evaluator_status_breakdown_still_populated():
    audit = DailyAudit()
    for status, fs in [
        (EvaluatorStatus.PICK, MatchFinalStatus.PICK_ALERT_SENT),
        (EvaluatorStatus.WATCHLIST, MatchFinalStatus.WATCHLIST),
        (EvaluatorStatus.NO_PICK, MatchFinalStatus.NO_PICK),
        (EvaluatorStatus.BLOCKED_MODEL, MatchFinalStatus.BLOCKED_MODEL),
        (EvaluatorStatus.BLOCKED_VALIDATION, MatchFinalStatus.BLOCKED_VALIDATION),
    ]:
        audit.record_match_result(_make_result(fs, eval_status=status, alert_decision=None
            if status != EvaluatorStatus.PICK else _make_ad(AlertStatus.SENT)))
    assert audit.evaluator_status_breakdown["PICK"] == 1
    assert audit.evaluator_status_breakdown["WATCHLIST"] == 1
    assert audit.evaluator_status_breakdown["NO_PICK"] == 1
    assert audit.evaluator_status_breakdown["BLOCKED_MODEL"] == 1
    assert audit.evaluator_status_breakdown["BLOCKED_VALIDATION"] == 1
