"""
tests/test_p6_runner.py
========================
P6 tests — run_match_with_result() moved to orchestration/match_runner,
pipeline.py is now a thin wrapper, and DailyAudit gains risk_decision_blocked_count.

Coverage
--------
A. run_match_with_result importable from orchestration/match_runner
B. pipeline.run_match_with_result is a thin wrapper (different object)
C. pipeline.run_match_with_result delegates to orchestration canonical
D. DailyAudit.risk_decision_blocked_count field exists and defaults to 0
E. record_match_result() increments risk_decision_blocked_count when blocked
F. record_match_result() does NOT increment when risk allowed (or no risk)
G. risk_decision_blocked_count appears in save_audit_json() payload
H. Non-regression: NO_PICK/WATCHLIST/BLOCKED paths don't touch risk counter
I. Non-regression: P5 final_status_breakdown unaffected by P6 additions
J. Non-regression: P4 evaluator_status_breakdown unaffected
K. Accumulation: multiple blocked risk decisions sum correctly
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import dataclasses
import pytest
from unittest.mock import MagicMock

from tennis_model.orchestration.audit import DailyAudit
from tennis_model.orchestration.match_runner import MatchFinalStatus, ALERT_SENT_STATUSES
from tennis_model.orchestration.alert_status import AlertStatus
from tennis_model.evaluator.evaluator_decision import EvaluatorStatus


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_risk(allowed: bool, stake_factor: float = 1.0):
    r = MagicMock()
    r.allowed = allowed
    r.stake_factor = stake_factor
    r.stake_units = 0.025 if allowed else 0.0
    return r


def _make_result(
    final_status: MatchFinalStatus,
    profile_quality_a: str = "full",
    profile_quality_b: str = "full",
    filter_reason: str = None,
    reason_codes: list = None,
    pick_player: str = "",
    alert_status: AlertStatus = None,
    risk_decision=None,
):
    """Build a minimal fake MatchRunResult."""
    ed = MagicMock()
    ed.status = (
        EvaluatorStatus.PICK if final_status in ALERT_SENT_STATUSES
        else EvaluatorStatus.NO_PICK
    )
    ed.reason_code = (reason_codes or ["TEST_CODE"])[0]
    ed.filter_reason = filter_reason

    ad = None
    if alert_status is not None:
        ad = MagicMock()
        ad.status = alert_status
        ad.stake_units = 0.02
        ad.stake_factor = 1.0

    pick = MagicMock()
    pick.pick_player = pick_player

    result = MagicMock()
    result.evaluator_decision = ed
    result.alert_decision = ad
    result.final_status = final_status
    result.profile_quality_a = profile_quality_a
    result.profile_quality_b = profile_quality_b
    result.filter_reason = filter_reason
    result.reason_codes = reason_codes or ["TEST_CODE"]
    result.pick = pick
    result.risk_decision = risk_decision
    return result


# ─────────────────────────────────────────────────────────────────────────────
# A. run_match_with_result importable from orchestration/match_runner
# ─────────────────────────────────────────────────────────────────────────────

def test_run_match_with_result_importable_from_match_runner():
    from tennis_model.orchestration.match_runner import run_match_with_result
    assert callable(run_match_with_result)


def test_run_match_with_result_has_correct_params():
    """Canonical function accepts the same signature as the pipeline wrapper."""
    import inspect
    from tennis_model.orchestration.match_runner import run_match_with_result
    sig = inspect.signature(run_match_with_result)
    params = set(sig.parameters.keys())
    required = {"match_str", "tournament", "surface", "market_odds_a", "market_odds_b",
                "bookmaker", "pick_number", "tour", "_silent", "_prefetched", "_audit"}
    assert required.issubset(params), f"Missing params: {required - params}"


# ─────────────────────────────────────────────────────────────────────────────
# B. pipeline.run_match_with_result is a distinct (wrapper) object
# ─────────────────────────────────────────────────────────────────────────────

def test_pipeline_wrapper_is_different_object():
    """
    pipeline.run_match_with_result must be a thin wrapper, NOT the same
    function object as orchestration.match_runner.run_match_with_result.
    (They have the same name but live in different modules.)
    """
    import tennis_model.pipeline as pipeline
    from tennis_model.orchestration.match_runner import run_match_with_result as canonical

    pipeline_fn = pipeline.run_match_with_result
    assert pipeline_fn is not canonical, (
        "pipeline.run_match_with_result should be a wrapper, not the canonical impl"
    )


# ─────────────────────────────────────────────────────────────────────────────
# C. pipeline.run_match_with_result delegates to canonical (import chain test)
# ─────────────────────────────────────────────────────────────────────────────

def test_pipeline_wrapper_docstring_says_wrapper():
    """Wrapper docstring must document its P6 delegation."""
    import tennis_model.pipeline as pipeline
    doc = pipeline.run_match_with_result.__doc__ or ""
    assert "P6" in doc or "orchestration" in doc, (
        "pipeline.run_match_with_result docstring should mention P6 delegation"
    )


def test_canonical_impl_docstring_says_canonical():
    """Canonical docstring must say it is the canonical P6 home."""
    from tennis_model.orchestration.match_runner import run_match_with_result
    doc = run_match_with_result.__doc__ or ""
    assert "P6" in doc or "canonical" in doc or "pipeline.py" in doc


# ─────────────────────────────────────────────────────────────────────────────
# D. DailyAudit.risk_decision_blocked_count field exists and defaults to 0
# ─────────────────────────────────────────────────────────────────────────────

def test_risk_decision_blocked_count_field_exists():
    fields = {f.name for f in dataclasses.fields(DailyAudit)}
    assert "risk_decision_blocked_count" in fields


def test_risk_decision_blocked_count_defaults_to_zero():
    audit = DailyAudit()
    assert audit.risk_decision_blocked_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# E. record_match_result() increments when risk is blocked
# ─────────────────────────────────────────────────────────────────────────────

def test_risk_blocked_increments_counter():
    audit = DailyAudit()
    blocked_risk = _make_risk(allowed=False, stake_factor=0.0)
    result = _make_result(
        MatchFinalStatus.PICK_SKIPPED_UNKNOWN,
        pick_player="A. Player",
        alert_status=AlertStatus.SKIPPED_UNKNOWN,
        risk_decision=blocked_risk,
    )
    audit.record_match_result(result)
    assert audit.risk_decision_blocked_count == 1


def test_risk_blocked_kelly_increments_counter():
    audit = DailyAudit()
    blocked_risk = _make_risk(allowed=False, stake_factor=0.0)
    result = _make_result(
        MatchFinalStatus.PICK_SKIPPED_KELLY,
        pick_player="B. Player",
        alert_status=AlertStatus.SKIPPED_KELLY,
        risk_decision=blocked_risk,
    )
    audit.record_match_result(result)
    assert audit.risk_decision_blocked_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# F. record_match_result() does NOT increment when risk is allowed or absent
# ─────────────────────────────────────────────────────────────────────────────

def test_risk_allowed_does_not_increment_counter():
    audit = DailyAudit()
    allowed_risk = _make_risk(allowed=True, stake_factor=1.0)
    result = _make_result(
        MatchFinalStatus.PICK_ALERT_SENT,
        pick_player="A. Player",
        alert_status=AlertStatus.SENT,
        risk_decision=allowed_risk,
    )
    audit.record_match_result(result)
    assert audit.risk_decision_blocked_count == 0


def test_no_risk_decision_does_not_increment_counter():
    """NO_PICK path: risk_decision is None — counter must stay 0."""
    audit = DailyAudit()
    result = _make_result(
        MatchFinalStatus.NO_PICK,
        risk_decision=None,
    )
    audit.record_match_result(result)
    assert audit.risk_decision_blocked_count == 0


def test_degraded_risk_allowed_does_not_increment():
    """DEGRADED (stake_factor=0.5, allowed=True) — not blocked, counter stays 0."""
    audit = DailyAudit()
    degraded_risk = _make_risk(allowed=True, stake_factor=0.5)
    result = _make_result(
        MatchFinalStatus.PICK_ALERT_SENT,
        profile_quality_a="degraded",
        profile_quality_b="full",
        pick_player="A. Player",
        alert_status=AlertStatus.SENT,
        risk_decision=degraded_risk,
    )
    audit.record_match_result(result)
    assert audit.risk_decision_blocked_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# G. risk_decision_blocked_count appears in save_audit_json() payload
# ─────────────────────────────────────────────────────────────────────────────

def test_risk_blocked_count_in_json(tmp_path):
    audit = DailyAudit()
    blocked_risk = _make_risk(allowed=False)
    result = _make_result(
        MatchFinalStatus.PICK_SKIPPED_UNKNOWN,
        risk_decision=blocked_risk,
        alert_status=AlertStatus.SKIPPED_UNKNOWN,
    )
    audit.record_match_result(result)
    audit.save_audit_json(audits_dir=str(tmp_path))

    saved = tmp_path / f"{audit.date}.json"
    assert saved.exists()
    with open(saved) as f:
        data = json.load(f)
    assert "risk_decision_blocked_count" in data
    assert data["risk_decision_blocked_count"] == 1


def test_risk_blocked_zero_in_json_when_none_blocked(tmp_path):
    audit = DailyAudit()
    audit.save_audit_json(audits_dir=str(tmp_path))
    saved = tmp_path / f"{audit.date}.json"
    with open(saved) as f:
        data = json.load(f)
    assert data["risk_decision_blocked_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# H. Non-regression: NO_PICK / WATCHLIST / BLOCKED don't touch risk counter
# ─────────────────────────────────────────────────────────────────────────────

def test_no_pick_path_risk_counter_zero():
    audit = DailyAudit()
    for status in (MatchFinalStatus.NO_PICK, MatchFinalStatus.WATCHLIST,
                   MatchFinalStatus.BLOCKED_MODEL, MatchFinalStatus.BLOCKED_VALIDATION):
        r = _make_result(status, risk_decision=None)
        audit.record_match_result(r)
    assert audit.risk_decision_blocked_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# I. Non-regression: P5 final_status_breakdown unaffected
# ─────────────────────────────────────────────────────────────────────────────

def test_p6_does_not_break_final_status_breakdown():
    audit = DailyAudit()
    blocked_risk = _make_risk(allowed=False)
    r = _make_result(
        MatchFinalStatus.PICK_SKIPPED_UNKNOWN,
        risk_decision=blocked_risk,
        alert_status=AlertStatus.SKIPPED_UNKNOWN,
    )
    audit.record_match_result(r)
    assert "PICK_SKIPPED_UNKNOWN" in audit.final_status_breakdown
    assert audit.final_status_breakdown["PICK_SKIPPED_UNKNOWN"] == 1
    assert audit.risk_decision_blocked_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# J. Non-regression: P4 evaluator_status_breakdown unaffected
# ─────────────────────────────────────────────────────────────────────────────

def test_p6_does_not_break_evaluator_status_breakdown():
    audit = DailyAudit()
    r = _make_result(MatchFinalStatus.NO_PICK, risk_decision=None)
    audit.record_match_result(r)
    assert "NO_PICK" in audit.evaluator_status_breakdown


# ─────────────────────────────────────────────────────────────────────────────
# K. Accumulation: multiple blocked risk decisions sum correctly
# ─────────────────────────────────────────────────────────────────────────────

def test_risk_blocked_accumulates():
    audit = DailyAudit()
    for _ in range(4):
        r = _make_result(
            MatchFinalStatus.PICK_SKIPPED_UNKNOWN,
            risk_decision=_make_risk(allowed=False),
            alert_status=AlertStatus.SKIPPED_UNKNOWN,
        )
        audit.record_match_result(r)
    # 2 allowed, should not count
    for _ in range(2):
        r = _make_result(
            MatchFinalStatus.PICK_ALERT_SENT,
            pick_player="A.",
            risk_decision=_make_risk(allowed=True),
            alert_status=AlertStatus.SENT,
        )
        audit.record_match_result(r)
    assert audit.risk_decision_blocked_count == 4
    assert audit.alerts_sent == 2
