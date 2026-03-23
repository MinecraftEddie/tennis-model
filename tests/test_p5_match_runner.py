"""
tests/test_p5_match_runner.py
==============================
P5 tests — MatchFinalStatus enum, MatchRunResult dataclass,
and build_final_status() mapping logic.

Coverage:
  A. MatchFinalStatus — enum membership and str equality
  B. build_final_status() — all EvaluatorStatus × AlertStatus combinations
  C. MatchRunResult — dataclass instantiation and field defaults
  D. Non-regression: Evaluator-blocked statuses map correctly
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock

from tennis_model.orchestration.match_runner import (
    MatchFinalStatus,
    MatchRunResult,
    build_final_status,
    ALERT_SENT_STATUSES,
    EVALUATOR_BLOCKED_STATUSES,
)
from tennis_model.evaluator.evaluator_decision import EvaluatorStatus
from tennis_model.orchestration.alert_status import AlertStatus


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ed(status: EvaluatorStatus, rec_action: str = "send"):
    """Fake EvaluatorDecision with the given status."""
    d = MagicMock()
    d.status = status
    d.recommended_action = rec_action
    d.reason_code = "PICK_APPROVED"
    d.filter_reason = None
    return d


def _ad(status: AlertStatus, stake_units: float = 1.0, stake_factor: float = 1.0):
    """Fake AlertDecision with the given status."""
    d = MagicMock()
    d.status = status
    d.stake_units = stake_units
    d.stake_factor = stake_factor
    return d


# ── A. MatchFinalStatus enum ──────────────────────────────────────────────────

def test_match_final_status_is_str_enum():
    assert isinstance(MatchFinalStatus.PICK_ALERT_SENT, str)
    assert MatchFinalStatus.PICK_ALERT_SENT == "PICK_ALERT_SENT"
    assert MatchFinalStatus.NO_PICK == "NO_PICK"
    assert MatchFinalStatus.FAILED == "FAILED"


def test_match_final_status_all_members():
    expected = {
        "PICK_ALERT_SENT", "PICK_DRY_RUN", "PICK_SUPPRESSED", "PICK_FAILED",
        "PICK_SKIPPED_UNKNOWN", "PICK_SKIPPED_KELLY", "PICK_SKIPPED_RISK",
        "PICK_SKIPPED_DEDUPE",
        "WATCHLIST", "BLOCKED_MODEL",
        "NO_PICK", "BLOCKED_VALIDATION",
        "FAILED",
    }
    actual = {s.value for s in MatchFinalStatus}
    assert actual == expected


def test_alert_sent_statuses_are_all_pick_path():
    for s in ALERT_SENT_STATUSES:
        assert s.value.startswith("PICK_"), f"{s} should start with PICK_"


def test_evaluator_blocked_statuses():
    assert MatchFinalStatus.WATCHLIST in EVALUATOR_BLOCKED_STATUSES
    assert MatchFinalStatus.BLOCKED_MODEL in EVALUATOR_BLOCKED_STATUSES
    assert MatchFinalStatus.PICK_ALERT_SENT not in EVALUATOR_BLOCKED_STATUSES
    assert MatchFinalStatus.NO_PICK not in EVALUATOR_BLOCKED_STATUSES


# ── B. build_final_status() ───────────────────────────────────────────────────

class TestBuildFinalStatus:
    """Non-PICK evaluator statuses map directly — no AlertDecision needed."""

    def test_watchlist(self):
        fs = build_final_status(_ed(EvaluatorStatus.WATCHLIST))
        assert fs == MatchFinalStatus.WATCHLIST

    def test_no_pick(self):
        fs = build_final_status(_ed(EvaluatorStatus.NO_PICK))
        assert fs == MatchFinalStatus.NO_PICK

    def test_blocked_validation(self):
        fs = build_final_status(_ed(EvaluatorStatus.BLOCKED_VALIDATION))
        assert fs == MatchFinalStatus.BLOCKED_VALIDATION

    def test_blocked_model(self):
        fs = build_final_status(_ed(EvaluatorStatus.BLOCKED_MODEL))
        assert fs == MatchFinalStatus.BLOCKED_MODEL

    # PICK path — AlertDecision determines outcome
    def test_pick_alert_sent(self):
        fs = build_final_status(_ed(EvaluatorStatus.PICK), _ad(AlertStatus.SENT))
        assert fs == MatchFinalStatus.PICK_ALERT_SENT

    def test_pick_dry_run(self):
        fs = build_final_status(_ed(EvaluatorStatus.PICK), _ad(AlertStatus.DRY_RUN))
        assert fs == MatchFinalStatus.PICK_DRY_RUN

    def test_pick_suppressed(self):
        fs = build_final_status(_ed(EvaluatorStatus.PICK), _ad(AlertStatus.SUPPRESSED))
        assert fs == MatchFinalStatus.PICK_SUPPRESSED

    def test_pick_failed(self):
        fs = build_final_status(_ed(EvaluatorStatus.PICK), _ad(AlertStatus.FAILED))
        assert fs == MatchFinalStatus.PICK_FAILED

    def test_pick_skipped_unknown(self):
        fs = build_final_status(_ed(EvaluatorStatus.PICK), _ad(AlertStatus.SKIPPED_UNKNOWN))
        assert fs == MatchFinalStatus.PICK_SKIPPED_UNKNOWN

    def test_pick_skipped_kelly(self):
        fs = build_final_status(_ed(EvaluatorStatus.PICK), _ad(AlertStatus.SKIPPED_KELLY))
        assert fs == MatchFinalStatus.PICK_SKIPPED_KELLY

    def test_pick_skipped_risk(self):
        fs = build_final_status(_ed(EvaluatorStatus.PICK), _ad(AlertStatus.SKIPPED_RISK))
        assert fs == MatchFinalStatus.PICK_SKIPPED_RISK

    def test_pick_skipped_dedupe(self):
        fs = build_final_status(_ed(EvaluatorStatus.PICK), _ad(AlertStatus.SKIPPED_DEDUPE))
        assert fs == MatchFinalStatus.PICK_SKIPPED_DEDUPE

    def test_pick_skipped_no_pick_maps_to_no_pick(self):
        fs = build_final_status(_ed(EvaluatorStatus.PICK), _ad(AlertStatus.SKIPPED_NO_PICK))
        assert fs == MatchFinalStatus.NO_PICK

    def test_pick_without_alert_decision_is_guarded(self):
        """If PICK path but alert_decision is None (bug guard), return NO_PICK."""
        fs = build_final_status(_ed(EvaluatorStatus.PICK), alert_decision=None)
        assert fs == MatchFinalStatus.NO_PICK

    def test_pick_unknown_alert_status_falls_back(self):
        """Unknown AlertStatus falls back to NO_PICK gracefully."""
        fake = MagicMock()
        fake.status = "SOMETHING_NEW_IN_FUTURE"
        fs = build_final_status(_ed(EvaluatorStatus.PICK), fake)
        assert fs == MatchFinalStatus.NO_PICK


# ── C. MatchRunResult — dataclass ─────────────────────────────────────────────

def test_match_run_result_minimal():
    ed = _ed(EvaluatorStatus.NO_PICK)
    result = MatchRunResult(
        match_id="2026-03-23_djokovic_sinner",
        player_a="Djokovic",
        player_b="Sinner",
        profile_quality_a="full",
        profile_quality_b="full",
        evaluator_decision=ed,
        final_status=MatchFinalStatus.NO_PICK,
    )
    assert result.match_id == "2026-03-23_djokovic_sinner"
    assert result.final_status == MatchFinalStatus.NO_PICK
    assert result.pick is None
    assert result.alert_decision is None
    assert result.risk_decision is None
    assert result.filter_reason is None
    assert result.reason_codes == []


def test_match_run_result_full_pick():
    ed = _ed(EvaluatorStatus.PICK)
    ad = _ad(AlertStatus.SENT, stake_units=0.8)
    pick = MagicMock()
    result = MatchRunResult(
        match_id="2026-03-23_etcheverry_medvedev",
        player_a="Etcheverry",
        player_b="Medvedev",
        profile_quality_a="degraded",
        profile_quality_b="full",
        evaluator_decision=ed,
        final_status=MatchFinalStatus.PICK_ALERT_SENT,
        reason_codes=["PICK_APPROVED"],
        alert_decision=ad,
        pick=pick,
        filter_reason=None,
    )
    assert result.final_status == MatchFinalStatus.PICK_ALERT_SENT
    assert result.alert_decision is ad
    assert result.pick is pick
    assert result.profile_quality_a == "degraded"


def test_match_run_result_watchlist():
    ed = _ed(EvaluatorStatus.WATCHLIST, rec_action="watchlist")
    result = MatchRunResult(
        match_id="2026-03-23_a_b",
        player_a="A",
        player_b="B",
        profile_quality_a="full",
        profile_quality_b="full",
        evaluator_decision=ed,
        final_status=MatchFinalStatus.WATCHLIST,
        filter_reason="EVALUATOR_WATCHLIST",
    )
    assert result.final_status == MatchFinalStatus.WATCHLIST
    assert result.alert_decision is None   # no alert in watchlist path
    assert result.filter_reason == "EVALUATOR_WATCHLIST"


# ── D. Non-regression: Etcheverry degraded profile scenario ──────────────────

def test_degraded_profile_pick_sent():
    """
    Etcheverry-style: degraded profile → PICK path entered → stake halved → SENT.
    PICK_ALERT_SENT should be the final_status (not blocked by degraded).
    """
    ed = _ed(EvaluatorStatus.PICK)
    ad = _ad(AlertStatus.SENT, stake_units=0.5, stake_factor=0.5)
    result = MatchRunResult(
        match_id="2026-03-23_etcheverry_x",
        player_a="Etcheverry",
        player_b="X",
        profile_quality_a="degraded",
        profile_quality_b="full",
        evaluator_decision=ed,
        final_status=build_final_status(ed, ad),
        alert_decision=ad,
        pick=MagicMock(),
    )
    assert result.final_status == MatchFinalStatus.PICK_ALERT_SENT


def test_unknown_profile_skipped():
    """UNKNOWN profile → risk engine blocks → PICK_SKIPPED_UNKNOWN."""
    ed = _ed(EvaluatorStatus.PICK)
    ad = _ad(AlertStatus.SKIPPED_UNKNOWN, stake_units=0.0, stake_factor=0.0)
    fs = build_final_status(ed, ad)
    assert fs == MatchFinalStatus.PICK_SKIPPED_UNKNOWN


def test_blocked_model_is_in_evaluator_blocked():
    """BLOCKED_MODEL from evaluator → qualified-only bucket in scan_today."""
    assert MatchFinalStatus.BLOCKED_MODEL in EVALUATOR_BLOCKED_STATUSES


def test_watchlist_is_in_evaluator_blocked():
    assert MatchFinalStatus.WATCHLIST in EVALUATOR_BLOCKED_STATUSES
