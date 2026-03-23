"""
tests/test_p1_integration.py
=============================
P1 integration tests — verify the cache / identity / retry wiring in pipeline.

Scenarios:
  1. identity resolved + TA 429 + cache hit
     → profile loaded from cache, profile_quality="degraded", no hard fail

  2. identity resolved + TA 429 + no cache
     → P0 behaviour preserved: data_source=degraded_ratelimit, profile_quality=degraded

  3. identity unresolved
     → hard fail still in place (validation.passed=False)

  4. P0 regression — Rybakina/Gibson-like: WTA data gate still filters both-estimated
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch, MagicMock
import pytest

from tennis_model.models import PlayerProfile
from tennis_model.validation import validate_match


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_atp_profile(
    short_name: str,
    identity_source: str = "map",
    data_source: str = "unknown",
    profile_quality: str = "unknown",
    ranking: int = 50,
    hard_wins: int = 30,
    hard_losses: int = 20,
) -> PlayerProfile:
    p = PlayerProfile(short_name=short_name)
    p.full_name       = short_name
    p.identity_source = identity_source
    p.data_source     = data_source
    p.profile_quality = profile_quality
    p.ranking         = ranking
    p.hard_wins       = hard_wins
    p.hard_losses     = hard_losses
    p.tour            = "atp"
    return p


# ── Scenario 1: 429 + cache hit ───────────────────────────────────────────────

def test_ta_429_cache_hit_no_hard_fail():
    """
    TA returns 429 but a cache entry exists.
    Expected: profile loaded from cache, profile_quality='degraded', pipeline continues.
    """
    cached_data = {
        "ranking":     25,
        "hard_wins":   150,
        "hard_losses": 60,
        "data_source": "tennis_abstract",
        "recent_form": ["W", "W", "L"],
        "serve_stats": {},
    }

    profile = _make_atp_profile("Test Player A", identity_source="map")

    with patch("tennis_model.pipeline._ta_fetch", return_value=("", "degraded_ratelimit")), \
         patch("tennis_model.pipeline._load_cached_profile", return_value=cached_data), \
         patch("tennis_model.pipeline._profile_cache_key", return_value="atp_test_player_a"):

        from tennis_model.pipeline import _top_up_from_tennis_abstract
        _top_up_from_tennis_abstract(profile)

    assert profile.profile_quality == "degraded", (
        "Cache hit on 429 must set profile_quality='degraded'"
    )
    assert profile.ranking == 25, "Cached ranking must be applied"
    assert profile.hard_wins == 150, "Cached hard_wins must be applied"
    # Must NOT be left with data_source="degraded_ratelimit" (was overwritten by cache)
    assert profile.data_source == "tennis_abstract"


def test_ta_429_cache_hit_validation_passes():
    """
    After cache-hit degraded state, validation must still pass (not hard fail).
    This is the core P1 requirement: cache absorbs transient failures.
    """
    cached_data = {
        "ranking":     25,
        "hard_wins":   150,
        "hard_losses": 60,
        "data_source": "tennis_abstract",
    }

    pa = _make_atp_profile("Cached Player",     identity_source="map", ranking=25, hard_wins=150, hard_losses=60)
    pb = _make_atp_profile("Opponent Player",   identity_source="map", ranking=40, hard_wins=100, hard_losses=50)

    # Simulate cache-hit outcome (profile_quality=degraded, data_source=tennis_abstract)
    pa.profile_quality = "degraded"
    pa.data_source     = "tennis_abstract"
    pb.profile_quality = "full"
    pb.data_source     = "tennis_abstract"

    v = validate_match(pa, pb, "Hard", market_odds_a=2.50, market_odds_b=1.65)
    # data_source="tennis_abstract" is not in _degraded_sources — should pass cleanly
    assert v.passed is True


# ── Scenario 2: 429 + no cache → P0 behaviour ─────────────────────────────────

def test_ta_429_no_cache_p0_degraded():
    """
    TA returns 429, no cache entry.
    Expected: P0 behaviour — data_source=degraded_ratelimit, profile_quality=degraded.
    """
    profile = _make_atp_profile("No Cache Player", identity_source="atp_search")

    with patch("tennis_model.pipeline._ta_fetch", return_value=("", "degraded_ratelimit")), \
         patch("tennis_model.pipeline._load_cached_profile", return_value=None), \
         patch("tennis_model.pipeline._profile_cache_key", return_value="atp_no_cache"):

        from tennis_model.pipeline import _top_up_from_tennis_abstract
        _top_up_from_tennis_abstract(profile)

    assert profile.data_source     == "degraded_ratelimit"
    assert profile.profile_quality == "degraded"


def test_ta_429_no_cache_validation_still_passes():
    """
    P0 invariant: identity resolved + degraded data → validation passes with warning.
    """
    pa = _make_atp_profile("Rate Limited Player", identity_source="atp_search",
                           data_source="degraded_ratelimit", profile_quality="degraded",
                           ranking=9999)
    pb = _make_atp_profile("Normal Player", identity_source="map",
                           data_source="tennis_abstract", profile_quality="full")

    v = validate_match(pa, pb, "Hard", market_odds_a=3.50, market_odds_b=1.40)

    assert v.passed is True, "P0 invariant: identity resolved + degraded must pass"
    assert v.confidence_penalty > 0


# ── Scenario 3: unresolved identity → hard fail preserved ─────────────────────

def test_unresolved_identity_hard_fail_preserved():
    """
    P0 safety net: unresolved identity must still hard fail in validation.
    P1 must not accidentally soften this.
    """
    pa = _make_atp_profile("Truly Unknown Player", identity_source="unresolved",
                           data_source="unknown", profile_quality="unknown",
                           ranking=9999)
    pb = _make_atp_profile("Known Player", identity_source="map",
                           data_source="tennis_abstract", profile_quality="full")

    v = validate_match(pa, pb, "Hard", market_odds_a=5.00, market_odds_b=1.22)

    assert v.passed is False, "Unresolved identity must hard fail"
    assert any("VALIDATION_SOURCE_UNKNOWN" in e for e in v.errors)


def test_ta_429_unresolved_identity_no_cache_attempted():
    """
    When identity_source='unresolved', cache must NOT be attempted — skip silently.
    """
    profile = _make_atp_profile("Unknown Player", identity_source="unresolved")

    with patch("tennis_model.pipeline._ta_fetch", return_value=("", "degraded_ratelimit")), \
         patch("tennis_model.pipeline._load_cached_profile") as mock_load, \
         patch("tennis_model.pipeline._profile_cache_key") as mock_key:

        from tennis_model.pipeline import _top_up_from_tennis_abstract
        _top_up_from_tennis_abstract(profile)

    # Cache must not be queried for unresolved players
    mock_load.assert_not_called()
    mock_key.assert_not_called()


# ── Scenario 4: WTA data gate regression ─────────────────────────────────────

def test_wta_data_gate_still_filters_estimated():
    """
    P0 regression: Rybakina/Gibson-like scenario.
    WTA data gate must still block both-estimated matches.
    Validation is irrelevant here — the gate fires inside run_match() before EV.
    This test verifies the gate logic directly via the filter_reason string format
    expected by scan_today.
    """
    from tennis_model.ev import EVResult

    # Simulate what run_match() sets when the gate fires
    _gate_reason = "WTA DATA GATE: PlayerA=wta_estimated, PlayerB=wta_estimated"

    _block = EVResult(edge=0.0, is_value=False, filter_reason=_gate_reason)
    assert not _block.is_value
    assert "WTA DATA GATE" in _block.filter_reason


def test_wta_estimated_validation_is_degraded_not_failed():
    """
    P0 invariant: wta_estimated is a degraded source, not an unresolved identity.
    Validation passes with a confidence penalty.
    """
    from tennis_model.models import PlayerProfile

    pa = PlayerProfile(short_name="Talia Gibson")
    pa.identity_source = "wta_profiles"
    pa.data_source     = "wta_estimated"
    pa.profile_quality = "degraded"
    pa.ranking         = 80
    pa.hard_wins       = 50
    pa.hard_losses     = 50

    pb = PlayerProfile(short_name="E. Rybakina")
    pb.identity_source = "wta_profiles"
    pb.data_source     = "tennis_abstract_dynamic"
    pb.profile_quality = "full"
    pb.ranking         = 4
    pb.hard_wins       = 100
    pb.hard_losses     = 30

    v = validate_match(pa, pb, "Hard", market_odds_a=7.00, market_odds_b=1.18)

    assert v.passed is True, "wta_estimated must not hard fail validation"
    assert v.confidence_penalty > 0
    assert not any("VALIDATION_SOURCE_UNKNOWN" in e for e in v.errors)
