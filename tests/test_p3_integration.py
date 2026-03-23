"""
tests/test_p3_integration.py
=============================
P3 integration tests — maybe_alert() return type + end-to-end alert decision flow.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from unittest.mock import patch, MagicMock

from tennis_model.models import PlayerProfile, MatchPick
from tennis_model.orchestration.alert_status import AlertStatus, AlertDecision
from tennis_model.quality.reason_codes import ReasonCode


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pick(pq_a: str = "full", pq_b: str = "full",
               stake: float = 0.04, tier: str = "CLEAN") -> MatchPick:
    pa = PlayerProfile(short_name="A. Player")
    pa.profile_quality = pq_a
    pa.identity_source = "map"
    pa.data_source = "tennis_abstract" if pq_a == "full" else "degraded_ratelimit"

    pb = PlayerProfile(short_name="B. Player")
    pb.profile_quality = pq_b
    pb.identity_source = "map"
    pb.data_source = "tennis_abstract" if pq_b == "full" else "degraded_ratelimit"

    pick = MatchPick(
        player_a=pa, player_b=pb, surface="Hard",
        prob_a=0.60, prob_b=0.40,
        market_odds_a=1.80, market_odds_b=2.20,
        pick_player="A. Player",
        stake_units=stake,
        quality_tier=tier,
    )
    return pick


# ── maybe_alert() returns AlertDecision ──────────────────────────────────────

def test_maybe_alert_returns_alert_decision_on_send():
    pick = _make_pick(pq_a="full", pq_b="full", stake=0.04)
    with patch("tennis_model.telegram.send_telegram", return_value=True), \
         patch("tennis_model.telegram.TELEGRAM_BOT_TOKEN", "real-token-value"), \
         patch("tennis_model.backtest.store_prediction"):
        from tennis_model.telegram import maybe_alert
        result = maybe_alert(pick, "card")
    assert isinstance(result, AlertDecision)
    assert result.status == AlertStatus.SENT
    assert result.telegram_sent is True
    assert result.stake_factor == 1.0


def test_maybe_alert_returns_alert_decision_on_telegram_failure():
    pick = _make_pick(pq_a="full", pq_b="full", stake=0.04)
    with patch("tennis_model.telegram.send_telegram", return_value=False), \
         patch("tennis_model.telegram.TELEGRAM_BOT_TOKEN", "real-token-value"), \
         patch("tennis_model.backtest.store_prediction"):
        from tennis_model.telegram import maybe_alert
        result = maybe_alert(pick, "card")
    assert isinstance(result, AlertDecision)
    assert result.status == AlertStatus.FAILED
    assert result.telegram_attempted is True
    assert result.telegram_sent is False


def test_maybe_alert_returns_dry_run_when_not_configured():
    pick = _make_pick(pq_a="full", pq_b="full", stake=0.04)
    with patch("tennis_model.telegram.send_telegram", return_value=False), \
         patch("tennis_model.telegram.TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE"), \
         patch("tennis_model.backtest.store_prediction"):
        from tennis_model.telegram import maybe_alert
        result = maybe_alert(pick, "card")
    assert isinstance(result, AlertDecision)
    assert result.status == AlertStatus.DRY_RUN
    assert result.telegram_attempted is False
    assert result.reason_code == ReasonCode.TELEGRAM_NOT_CONFIGURED


# ── UNKNOWN profile → SKIPPED_UNKNOWN ────────────────────────────────────────

def test_maybe_alert_unknown_returns_skipped_unknown():
    pick = _make_pick(pq_a="unknown", pq_b="full", stake=0.04)
    with patch("tennis_model.telegram.send_telegram") as mock_tg, \
         patch("tennis_model.backtest.store_prediction") as mock_store:
        from tennis_model.telegram import maybe_alert
        result = maybe_alert(pick, "card")
    assert isinstance(result, AlertDecision)
    assert result.status == AlertStatus.SKIPPED_UNKNOWN
    assert result.reason_code == ReasonCode.ALERT_SKIPPED_UNKNOWN
    assert result.telegram_attempted is False
    mock_tg.assert_not_called()
    mock_store.assert_not_called()


# ── DEGRADED → stake * 0.5 + SENT ────────────────────────────────────────────

def test_maybe_alert_degraded_halves_stake_and_returns_sent():
    pick = _make_pick(pq_a="degraded", pq_b="full", stake=0.04)
    with patch("tennis_model.telegram.send_telegram", return_value=True), \
         patch("tennis_model.telegram.TELEGRAM_BOT_TOKEN", "real-token-value"), \
         patch("tennis_model.backtest.store_prediction"):
        from tennis_model.telegram import maybe_alert
        result = maybe_alert(pick, "card")
    assert isinstance(result, AlertDecision)
    assert result.status == AlertStatus.SENT
    assert result.stake_factor == pytest.approx(0.5, abs=1e-6)
    assert result.stake_units == pytest.approx(0.02, abs=1e-6)
    assert pick.stake_units == pytest.approx(0.02, abs=1e-6)


# ── No pick_player → SKIPPED_NO_PICK ─────────────────────────────────────────

def test_maybe_alert_no_pick_player_returns_skipped():
    pick = _make_pick()
    pick.pick_player = None
    with patch("tennis_model.telegram.send_telegram") as mock_tg:
        from tennis_model.telegram import maybe_alert
        result = maybe_alert(pick, "card")
    assert result.status == AlertStatus.SKIPPED_NO_PICK
    mock_tg.assert_not_called()


# ── FRAGILE tier → SUPPRESSED ────────────────────────────────────────────────

def test_maybe_alert_fragile_tier_suppressed():
    pick = _make_pick(tier="FRAGILE")
    with patch("tennis_model.telegram.send_telegram") as mock_tg, \
         patch("tennis_model.backtest.store_prediction") as mock_store:
        from tennis_model.telegram import maybe_alert
        result = maybe_alert(pick, "card")
    assert result.status == AlertStatus.SUPPRESSED
    assert result.reason_code == ReasonCode.ALERT_SUPPRESSED_FRAGILE
    mock_tg.assert_not_called()
    mock_store.assert_not_called()


# ── Backward compat: P2 tests still pass (stake correctly halved) ─────────────

def test_p2_compat_degraded_stake_still_halved():
    """Regression: P2 behavior preserved — stake_units halved for DEGRADED."""
    pick = _make_pick(pq_a="degraded", pq_b="full", stake=0.04)
    with patch("tennis_model.telegram.send_telegram", return_value=True), \
         patch("tennis_model.telegram.TELEGRAM_BOT_TOKEN", "real-token-value"), \
         patch("tennis_model.backtest.store_prediction"):
        from tennis_model.telegram import maybe_alert
        maybe_alert(pick, "card")
    assert pick.stake_units == pytest.approx(0.02, abs=1e-5)


def test_p2_compat_unknown_suppresses_telegram():
    """Regression: P2 behavior preserved — UNKNOWN suppresses Telegram."""
    pick = _make_pick(pq_a="unknown", pq_b="full", stake=0.04)
    with patch("tennis_model.telegram.send_telegram") as mock_tg, \
         patch("tennis_model.backtest.store_prediction") as mock_store:
        from tennis_model.telegram import maybe_alert
        maybe_alert(pick, "card")
    mock_tg.assert_not_called()
    mock_store.assert_not_called()
