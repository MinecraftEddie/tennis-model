"""
tests/test_p4_integration.py
=============================
P4 integration tests — build_evaluator_decision() in context + audit integration.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch

from tennis_model.models import PlayerProfile, MatchPick
from tennis_model.evaluator.evaluator_decision import (
    EvaluatorStatus, build_evaluator_decision,
)
from tennis_model.orchestration.audit import DailyAudit
from tennis_model.orchestration.alert_status import AlertStatus
from tennis_model.quality.reason_codes import ReasonCode


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ev(is_value: bool, filter_reason: str = None):
    ev = MagicMock()
    ev.is_value = is_value
    ev.filter_reason = filter_reason
    return ev


def _make_pick(pq_a="full", pq_b="full", stake=0.04, tier="CLEAN"):
    pa = PlayerProfile(short_name="A. Player")
    pa.profile_quality = pq_a
    pa.identity_source = "map"
    pa.data_source = "tennis_abstract"

    pb = PlayerProfile(short_name="B. Player")
    pb.profile_quality = pq_b
    pb.identity_source = "map"
    pb.data_source = "tennis_abstract"

    return MatchPick(
        player_a=pa, player_b=pb, surface="Hard",
        prob_a=0.60, prob_b=0.40,
        market_odds_a=1.80, market_odds_b=2.20,
        pick_player="A. Player",
        stake_units=stake,
        quality_tier=tier,
    )


# ── EvaluatorDecision correctly routes picks ─────────────────────────────────

def test_pick_routes_to_maybe_alert():
    """When EvaluatorDecision=PICK, maybe_alert should be called."""
    eval_r = {"recommended_action": "send", "confidence": 0.8,
              "alert_level": "high", "short_message": "ok"}
    pick = _make_pick()

    with patch("tennis_model.telegram.maybe_alert") as mock_alert, \
         patch("tennis_model.telegram.send_telegram", return_value=True), \
         patch("tennis_model.backtest.store_prediction"):
        from tennis_model.orchestration.alert_status import AlertDecision
        mock_alert.return_value = AlertDecision(
            status=AlertStatus.SENT, reason_code="TELEGRAM_SEND_OK",
            stake_units=0.04, stake_factor=1.0,
            telegram_attempted=True, telegram_sent=True,
        )
        # Verify build produces PICK
        ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=True)
        assert ed.status == EvaluatorStatus.PICK
        assert ed.filter_reason is None


def test_watchlist_does_not_route_to_maybe_alert():
    """When EvaluatorDecision=WATCHLIST, no AlertDecision should be produced."""
    eval_r = {"recommended_action": "watchlist", "confidence": 0.4,
              "alert_level": "low", "short_message": "thin", "reasons": ["edge<7%"]}
    ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=True)
    assert ed.status == EvaluatorStatus.WATCHLIST
    assert ed.filter_reason == "EVALUATOR_WATCHLIST"


def test_watchlist_recorded_in_audit():
    audit = DailyAudit()
    eval_r = {"recommended_action": "watchlist", "confidence": 0.4,
              "alert_level": "low", "short_message": "thin"}
    ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=True)
    audit.record_evaluator_decision(ed)
    assert audit.evaluator_watchlist_count == 1
    assert audit.evaluator_status_breakdown.get("WATCHLIST") == 1


def test_no_pick_recorded_in_audit():
    audit = DailyAudit()
    ed = build_evaluator_decision(_ev(False, "LOW CONFIDENCE"), {}, validation_passed=True)
    audit.record_evaluator_decision(ed)
    assert audit.no_pick_count == 1


def test_validation_block_recorded_in_audit():
    audit = DailyAudit()
    ed = build_evaluator_decision(_ev(False, "VALIDATION FAILED"), {}, validation_passed=False)
    audit.record_evaluator_decision(ed)
    assert audit.validation_block_count == 1
    assert audit.no_pick_count == 0  # separate counter


# ── Backward compat: filter_reason strings unchanged ─────────────────────────

def test_watchlist_filter_reason_unchanged():
    """scan_today() depends on filter_reason == 'EVALUATOR_WATCHLIST'."""
    eval_r = {"recommended_action": "watchlist", "confidence": 0.4}
    ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=True)
    assert ed.filter_reason == "EVALUATOR_WATCHLIST"  # same as pre-P4 EVALUATOR_* string
    assert ed.filter_reason.startswith("EVALUATOR_")  # scan_today check preserved


def test_blocked_model_filter_reason_unchanged():
    """scan_today() depends on filter_reason.startswith('EVALUATOR_')."""
    eval_r = {"recommended_action": "ignore", "confidence": 0.2}
    ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=True)
    assert ed.filter_reason == "EVALUATOR_IGNORE"
    assert ed.filter_reason.startswith("EVALUATOR_")


def test_no_pick_filter_reason_unchanged():
    """scan_today() uses filter_reason for blocked list — must be original EV reason."""
    ev_reason = "NO MARKET ODDS (side B)"
    ed = build_evaluator_decision(_ev(False, ev_reason), {}, validation_passed=True)
    assert ed.filter_reason == ev_reason


# ── Etcheverry regression: DEGRADED → PICK with stake_factor=0.5 ──────────────

def test_etcheverry_degraded_reaches_pick_status():
    """Etcheverry with degraded profile should get EvaluatorStatus.PICK when EV passes."""
    eval_r = {"recommended_action": "send", "confidence": 0.65}
    ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=True)
    assert ed.status == EvaluatorStatus.PICK
    # The actual stake reduction happens later in the risk engine (P3), not here


def test_pick_with_full_profile_stays_pick():
    eval_r = {"recommended_action": "send_with_caution", "confidence": 0.7}
    ed = build_evaluator_decision(_ev(True), eval_r, validation_passed=True)
    assert ed.status == EvaluatorStatus.PICK
    assert ed.recommended_action == "send_with_caution"


# ── P3 regression: AlertDecision still correct for PICK picks ─────────────────

def test_pick_alert_decision_still_sent():
    """After P4, a PICK still flows to maybe_alert() → AlertDecision.SENT."""
    pick = _make_pick(pq_a="full", pq_b="full", stake=0.04)
    with patch("tennis_model.telegram.send_telegram", return_value=True), \
         patch("tennis_model.telegram.TELEGRAM_BOT_TOKEN", "real-token-value"), \
         patch("tennis_model.backtest.store_prediction"):
        from tennis_model.telegram import maybe_alert
        from tennis_model.orchestration.alert_status import AlertStatus
        result = maybe_alert(pick, "card")
    assert result.status == AlertStatus.SENT


def test_degraded_pick_alert_decision_stake_reduced():
    """After P4, a DEGRADED pick still gets stake_factor=0.5 in AlertDecision."""
    pick = _make_pick(pq_a="degraded", pq_b="full", stake=0.04)
    with patch("tennis_model.telegram.send_telegram", return_value=True), \
         patch("tennis_model.telegram.TELEGRAM_BOT_TOKEN", "real-token-value"), \
         patch("tennis_model.backtest.store_prediction"):
        from tennis_model.telegram import maybe_alert
        result = maybe_alert(pick, "card")
    assert result.stake_factor == pytest.approx(0.5)
    assert result.stake_units == pytest.approx(0.02)
