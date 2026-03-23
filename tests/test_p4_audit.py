"""
tests/test_p4_audit.py
=======================
P4 tests — DailyAudit.record_evaluator_decision() and P4 JSON export.
"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock
from tennis_model.orchestration.audit import DailyAudit
from tennis_model.evaluator.evaluator_decision import EvaluatorStatus, EvaluatorDecision
from tennis_model.quality.reason_codes import ReasonCode


def _ed(status: EvaluatorStatus) -> EvaluatorDecision:
    return EvaluatorDecision(
        status=status,
        reason_code="TEST",
        filter_reason="TEST_FILTER" if status != EvaluatorStatus.PICK else None,
        confidence=0.7,
        message="test",
        recommended_action="send" if status == EvaluatorStatus.PICK else "blocked",
    )


# ── record_evaluator_decision increments named counters ──────────────────────

def test_no_pick_increments():
    audit = DailyAudit()
    audit.record_evaluator_decision(_ed(EvaluatorStatus.NO_PICK))
    assert audit.no_pick_count == 1
    assert audit.evaluator_status_breakdown.get("NO_PICK") == 1


def test_blocked_validation_increments():
    audit = DailyAudit()
    audit.record_evaluator_decision(_ed(EvaluatorStatus.BLOCKED_VALIDATION))
    assert audit.validation_block_count == 1
    assert audit.evaluator_status_breakdown.get("BLOCKED_VALIDATION") == 1


def test_blocked_model_increments():
    audit = DailyAudit()
    audit.record_evaluator_decision(_ed(EvaluatorStatus.BLOCKED_MODEL))
    assert audit.model_block_count == 1
    assert audit.evaluator_status_breakdown.get("BLOCKED_MODEL") == 1


def test_watchlist_increments():
    audit = DailyAudit()
    audit.record_evaluator_decision(_ed(EvaluatorStatus.WATCHLIST))
    assert audit.evaluator_watchlist_count == 1
    assert audit.evaluator_status_breakdown.get("WATCHLIST") == 1


def test_pick_no_named_counter_incremented():
    """PICK status has no named counter — only the breakdown dict."""
    audit = DailyAudit()
    audit.record_evaluator_decision(_ed(EvaluatorStatus.PICK))
    assert audit.no_pick_count == 0
    assert audit.validation_block_count == 0
    assert audit.model_block_count == 0
    assert audit.evaluator_watchlist_count == 0
    assert audit.evaluator_status_breakdown.get("PICK") == 1


def test_multiple_decisions_breakdown():
    audit = DailyAudit()
    for _ in range(3):
        audit.record_evaluator_decision(_ed(EvaluatorStatus.NO_PICK))
    audit.record_evaluator_decision(_ed(EvaluatorStatus.WATCHLIST))
    audit.record_evaluator_decision(_ed(EvaluatorStatus.PICK))
    assert audit.no_pick_count == 3
    assert audit.evaluator_watchlist_count == 1
    assert audit.evaluator_status_breakdown["NO_PICK"] == 3
    assert audit.evaluator_status_breakdown["WATCHLIST"] == 1
    assert audit.evaluator_status_breakdown["PICK"] == 1


# ── P4 fields in JSON export ─────────────────────────────────────────────────

def test_audit_json_includes_p4_fields():
    audit = DailyAudit()
    audit.record_evaluator_decision(_ed(EvaluatorStatus.NO_PICK))
    audit.record_evaluator_decision(_ed(EvaluatorStatus.WATCHLIST))
    audit.record_evaluator_decision(_ed(EvaluatorStatus.BLOCKED_VALIDATION))

    with tempfile.TemporaryDirectory() as tmp:
        audit.save_audit_json(audits_dir=tmp)
        path = os.path.join(tmp, f"{audit.date}.json")
        with open(path) as f:
            data = json.load(f)

    assert "no_pick_count" in data
    assert "validation_block_count" in data
    assert "model_block_count" in data
    assert "evaluator_watchlist_count" in data
    assert "evaluator_status_breakdown" in data
    assert data["no_pick_count"] == 1
    assert data["evaluator_watchlist_count"] == 1
    assert data["validation_block_count"] == 1
    assert data["evaluator_status_breakdown"]["NO_PICK"] == 1
    assert data["evaluator_status_breakdown"]["WATCHLIST"] == 1


# ── P3 and P4 counters coexist without conflict ───────────────────────────────

def test_p3_and_p4_counters_coexist():
    """record_alert_decision and record_evaluator_decision are independent."""
    from tennis_model.orchestration.alert_status import AlertStatus, AlertDecision
    audit = DailyAudit()

    # P4: record evaluator decision
    audit.record_evaluator_decision(_ed(EvaluatorStatus.WATCHLIST))

    # P3: record alert decision (would be for a PICK that was processed)
    alert_d = AlertDecision(
        status=AlertStatus.DRY_RUN,
        reason_code="TELEGRAM_DRY_RUN",
        stake_units=0.02,
        stake_factor=0.5,
        telegram_attempted=False,
        telegram_sent=False,
    )
    audit.record_alert_decision(alert_d)

    assert audit.evaluator_watchlist_count == 1
    assert audit.alerts_dry_run == 1
    assert audit.stake_reduced_count == 1
