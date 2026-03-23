"""
tests/test_p4_evaluator_decision.py
=====================================
P4 tests — EvaluatorStatus enum, EvaluatorDecision dataclass,
and build_evaluator_decision() logic.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock

from tennis_model.evaluator.evaluator_decision import (
    EvaluatorStatus, EvaluatorDecision, build_evaluator_decision,
)
from tennis_model.quality.reason_codes import ReasonCode


# ── EvaluatorStatus is a str enum ────────────────────────────────────────────

def test_evaluator_status_is_str_enum():
    assert isinstance(EvaluatorStatus.PICK, str)
    assert EvaluatorStatus.PICK == "PICK"
    assert EvaluatorStatus.WATCHLIST == "WATCHLIST"
    assert EvaluatorStatus.NO_PICK == "NO_PICK"
    assert EvaluatorStatus.BLOCKED_VALIDATION == "BLOCKED_VALIDATION"
    assert EvaluatorStatus.BLOCKED_MODEL == "BLOCKED_MODEL"


def test_evaluator_status_all_members():
    expected = {"PICK", "WATCHLIST", "NO_PICK", "BLOCKED_VALIDATION", "BLOCKED_MODEL"}
    actual = {s.value for s in EvaluatorStatus}
    assert actual == expected


# ── Helper: fake EVResult ─────────────────────────────────────────────────────

def _ev(is_value: bool, filter_reason: str = None):
    ev = MagicMock()
    ev.is_value = is_value
    ev.filter_reason = filter_reason
    return ev


# ── Rule 1: EV blocked → NO_PICK or BLOCKED_VALIDATION ───────────────────────

def test_ev_blocked_validation_failed():
    ed = build_evaluator_decision(_ev(False, "VALIDATION FAILED"), {}, validation_passed=False)
    assert ed.status == EvaluatorStatus.BLOCKED_VALIDATION
    assert ed.reason_code == ReasonCode.PICK_VALIDATION_FAILED
    assert ed.filter_reason == "VALIDATION FAILED"
    assert ed.recommended_action == "blocked"


def test_ev_blocked_no_validation_failure():
    ed = build_evaluator_decision(_ev(False, "No edge — LOW CONFIDENCE"), {}, validation_passed=True)
    assert ed.status == EvaluatorStatus.NO_PICK
    assert ed.filter_reason == "No edge — LOW CONFIDENCE"
    assert ed.recommended_action == "blocked"


def test_ev_blocked_no_pick_uses_reason_code():
    ed = build_evaluator_decision(_ev(False, "WTA DATA GATE: x=wta_estimated"), {}, validation_passed=True)
    assert ed.status == EvaluatorStatus.NO_PICK
    assert ed.reason_code == ReasonCode.PICK_WTA_DATA_GATE


def test_ev_blocked_no_filter_reason():
    """When filter_reason is None or empty, falls back to PICK_NO_EDGE."""
    ed = build_evaluator_decision(_ev(False, None), {}, validation_passed=True)
    assert ed.status == EvaluatorStatus.NO_PICK
    assert ed.reason_code == ReasonCode.PICK_NO_EDGE


# ── Rule 2: No evaluator → default PICK ──────────────────────────────────────

def test_no_evaluator_defaults_to_pick():
    """When eval_result={} (evaluator unavailable), EV passed → PICK."""
    ed = build_evaluator_decision(_ev(True), {}, validation_passed=True)
    assert ed.status == EvaluatorStatus.PICK
    assert ed.reason_code == ReasonCode.PICK_APPROVED
    assert ed.filter_reason is None


def test_no_rec_action_defaults_to_pick():
    ed = build_evaluator_decision(_ev(True), {"confidence": 0.7}, validation_passed=True)
    assert ed.status == EvaluatorStatus.PICK


# ── Rule 3: Evaluator → PICK ──────────────────────────────────────────────────

def test_evaluator_send_produces_pick():
    eval_r = {"recommended_action": "send", "confidence": 0.8, "short_message": "strong edge"}
    ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=True)
    assert ed.status == EvaluatorStatus.PICK
    assert ed.reason_code == ReasonCode.PICK_APPROVED
    assert ed.filter_reason is None
    assert ed.recommended_action == "send"
    assert ed.confidence == 0.8
    assert ed.message == "strong edge"


def test_evaluator_send_with_caution_produces_pick():
    eval_r = {"recommended_action": "send_with_caution", "confidence": 0.6, "short_message": "caution"}
    ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=True)
    assert ed.status == EvaluatorStatus.PICK
    assert ed.recommended_action == "send_with_caution"


# ── Rule 4: Evaluator → WATCHLIST ────────────────────────────────────────────

def test_evaluator_watchlist_produces_watchlist():
    eval_r = {"recommended_action": "watchlist", "confidence": 0.4, "short_message": "thin sample"}
    ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=True)
    assert ed.status == EvaluatorStatus.WATCHLIST
    assert ed.reason_code == ReasonCode.PICK_WATCHLIST
    assert ed.filter_reason == "EVALUATOR_WATCHLIST"   # backward-compat string
    assert ed.recommended_action == "watchlist"


# ── Rule 5: Evaluator → BLOCKED_MODEL ────────────────────────────────────────

def test_evaluator_ignore_produces_blocked_model():
    eval_r = {"recommended_action": "ignore", "confidence": 0.2, "short_message": "no value"}
    ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=True)
    assert ed.status == EvaluatorStatus.BLOCKED_MODEL
    assert ed.reason_code == ReasonCode.PICK_BLOCKED_MODEL
    assert ed.filter_reason == "EVALUATOR_IGNORE"   # backward-compat string


def test_evaluator_unknown_action_produces_blocked_model():
    eval_r = {"recommended_action": "unknown_future_action", "confidence": 0.3}
    ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=True)
    assert ed.status == EvaluatorStatus.BLOCKED_MODEL


# ── Priority: validation failure only matters when EV also blocked ────────────

def test_validation_failure_with_ev_passing_still_picks():
    """If EV passes despite validation failure penalty, we still pick."""
    eval_r = {"recommended_action": "send", "confidence": 0.65}
    ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=False)
    assert ed.status == EvaluatorStatus.PICK  # EV passed → evaluator decides


# ── EvaluatorDecision stores eval_result for compatibility ───────────────────

def test_evaluator_decision_stores_eval_result():
    eval_r = {"recommended_action": "send", "reasons": ["edge ok"], "risk_flags": []}
    ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=True)
    assert ed.eval_result is eval_r
    assert ed.eval_result.get("reasons") == ["edge ok"]
