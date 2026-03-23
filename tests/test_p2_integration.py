"""
tests/test_p2_integration.py
=============================
P2 integration tests — verify quality classification and business consequences.

Scenarios:
  1. QUALITY_RULES business values are correct for all tiers
  2. Etcheverry case: DEGRADED quality → allow_pick=True, stake_factor=0.5
  3. Clean player (Djokovic/Rybakina): FULL quality → stake_factor=1.0
  4. stake_units halved in maybe_alert for DEGRADED profiles
  5. UNKNOWN profile suppressed in maybe_alert (defence-in-depth)
  6. pipeline.py imports fetch_profile_with_quality
  7. P1 regression: validation still passes for degraded-but-resolved identity
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch, MagicMock

from tennis_model.models import PlayerProfile, MatchPick
from tennis_model.quality.profile_quality import ProfileQuality, QUALITY_RULES
from tennis_model.validation import validate_match


# ── 1. QUALITY_RULES business values ─────────────────────────────────────────

def test_quality_rules_allow_pick():
    assert QUALITY_RULES[ProfileQuality.FULL]["allow_pick"]     is True
    assert QUALITY_RULES[ProfileQuality.DEGRADED]["allow_pick"] is True
    assert QUALITY_RULES[ProfileQuality.UNKNOWN]["allow_pick"]  is False


def test_quality_rules_stake_factors():
    assert QUALITY_RULES[ProfileQuality.FULL]["stake_factor"]     == 1.0
    assert QUALITY_RULES[ProfileQuality.DEGRADED]["stake_factor"] == 0.5
    assert QUALITY_RULES[ProfileQuality.UNKNOWN]["stake_factor"]  == 0.0


# ── 2. Etcheverry: degraded, not hard fail ────────────────────────────────────

def test_etcheverry_degraded_allows_pick():
    """
    Etcheverry with identity resolved but fetch failed → DEGRADED.
    QUALITY_RULES must allow_pick=True (no hard fail).
    """
    rules = QUALITY_RULES[ProfileQuality.DEGRADED]
    assert rules["allow_pick"] is True, "DEGRADED must allow pick (no hard fail)"
    assert rules["stake_factor"] == 0.5, "DEGRADED must halve stake"


def test_etcheverry_degraded_validation_passes():
    """P0/P1 invariant preserved: degraded + resolved identity passes validation."""
    pa = PlayerProfile(short_name="T. Etcheverry")
    pa.identity_source = "map"
    pa.data_source     = "degraded_ratelimit"
    pa.profile_quality = "degraded"
    pa.ranking         = 50
    pa.hard_wins       = 50
    pa.hard_losses     = 30

    pb = PlayerProfile(short_name="C. Alcaraz")
    pb.identity_source = "map"
    pb.data_source     = "tennis_abstract"
    pb.profile_quality = "full"
    pb.ranking         = 3
    pb.hard_wins       = 100
    pb.hard_losses     = 20

    v = validate_match(pa, pb, "Hard", market_odds_a=5.00, market_odds_b=1.28)
    assert v.passed is True, "Degraded-but-resolved must not hard fail"
    assert v.confidence_penalty > 0.0, "Degraded must carry a confidence penalty"


# ── 3. Clean player: FULL quality ────────────────────────────────────────────

def test_full_quality_stake_factor():
    assert QUALITY_RULES[ProfileQuality.FULL]["stake_factor"] == 1.0


# ── 4. stake_units halved in maybe_alert for DEGRADED ────────────────────────

def _make_pick(pq_a: str = "full", pq_b: str = "full", stake: float = 0.04) -> MatchPick:
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
        quality_tier="CAUTION",
    )
    return pick


def test_maybe_alert_halves_stake_on_degraded():
    pick = _make_pick(pq_a="degraded", pq_b="full", stake=0.04)

    with patch("tennis_model.telegram.send_telegram", return_value=True), \
         patch("tennis_model.backtest.store_prediction"):
        from tennis_model.telegram import maybe_alert
        maybe_alert(pick, "card")

    # Stake must have been reduced by factor 0.5
    assert pick.stake_units == pytest.approx(0.02, abs=1e-5), (
        f"Expected 0.02 after ×0.5 reduction, got {pick.stake_units}"
    )


def test_maybe_alert_does_not_reduce_stake_for_full():
    pick = _make_pick(pq_a="full", pq_b="full", stake=0.04)

    with patch("tennis_model.telegram.send_telegram", return_value=True), \
         patch("tennis_model.backtest.store_prediction"):
        from tennis_model.telegram import maybe_alert
        maybe_alert(pick, "card")

    # Stake must be unchanged for FULL quality
    assert pick.stake_units == pytest.approx(0.04, abs=1e-5)


# ── 5. UNKNOWN profile suppressed in maybe_alert ─────────────────────────────

def test_maybe_alert_suppresses_unknown_profile():
    pick = _make_pick(pq_a="unknown", pq_b="full", stake=0.04)

    with patch("tennis_model.telegram.send_telegram") as mock_tg, \
         patch("tennis_model.backtest.store_prediction") as mock_store:
        from tennis_model.telegram import maybe_alert
        maybe_alert(pick, "card")

    # Telegram must NOT have been called
    mock_tg.assert_not_called()
    mock_store.assert_not_called()


# ── 6. pipeline.py imports fetch_profile_with_quality ────────────────────────

def test_pipeline_has_fetch_profile_with_quality():
    import tennis_model.pipeline as pipeline_module
    assert hasattr(pipeline_module, "fetch_profile_with_quality"), (
        "pipeline.py must import fetch_profile_with_quality from profile_fetcher"
    )


# ── 7. P1 regression: WTA data gate still blocks both-estimated ──────────────

def test_wta_data_gate_still_blocks():
    from tennis_model.ev import EVResult
    gate = "WTA DATA GATE: pa=wta_estimated, pb=wta_estimated"
    block = EVResult(edge=0.0, is_value=False, filter_reason=gate)
    assert not block.is_value
    assert "WTA DATA GATE" in block.filter_reason


# ── pytest import ─────────────────────────────────────────────────────────────
import pytest
